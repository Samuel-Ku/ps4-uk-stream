"""BambooUA provider (https://bambooua.com) — Ukrainian-dubbed anime,
doramas, lakorns, TV-shows and cinema. Issue #17, Group 1.

The upstream Kotlin parses a JSON-LD block (``JSONModel.kt``) for the
content metadata and a ``const playlist = [...]`` inline script for
the episode manifest. The same shapes are mirrored here as Pydantic
DTOs (``_JSONModel`` / ``_PlaylistGroup`` / ``_PlaylistEpisode``).
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field

from ..country import extract_country
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    StreamType,
    Translation,
)
from .base import BaseProvider, ProviderError, model_b_axes

BASE_URL = "https://bambooua.com"

# The upstream `mainPage = mainPageOf(...)` declares nine sections, but
# the dead `world-bl` listing (301 -> homepage, verified live 2026-08-09)
# is retired from the exposed set.
BAMBOUA_SECTIONS: tuple[Section, ...] = (
    Section(id="cinema", title="Фільми", form="movie"),
    Section(id="dorama", title="Дорами", form="series"),
    Section(id="anime", title="Аніме", styles=frozenset({"anime"})),
    Section(id="lakorn", title="Лакорн", form="series"),
    Section(id="voice", title="Озвучення", form="series"),
    Section(id="tv-show", title="ТВ-шоу", form="series"),
    Section(id="done", title="Завершені", form="series"),
    Section(id="now", title="Поточні", form="series"),
)

# URL path segment -> MediaType. Longest prefixes first so a longer
# needle (`tv-show`) wins over any future bare prefix, and `now`
# (single-segment) wins over any future longer prefix. The upstream
# maps: dorama -> AsianDrama, anime -> Anime, else -> Movie; but here
# we classify per-card so a `/cinema/` URL is movie and a `/dorama/`
# URL is series/dorama.
_PATH_TYPE: tuple[tuple[str, str], ...] = (
    ("tv-show", "series"),
    ("cinema", "movie"),
    ("dorama", "dorama"),
    ("anime", "anime"),
    ("lakorn", "series"),
    ("voice", "series"),
    ("done", "series"),
    ("now", "series"),
)

# Sentinel episode-id suffix for movies (whose playlist has a single
# file URL rather than a season/episode map).
MOVIE_SUFFIX = ":__movie__"

#: The site's subscription-gate placeholder: gated titles ("Для
#: підписників") are served this sponsor promo clip instead of the real
#: video — the real m3u8 is never present on the page for non-subscribers.
#: Any future variant keeps the "sponsor" marker in its path.
_SPONSOR_MARKER = "sponsor"


def _is_sponsor_file(path: str) -> bool:
    """True when ``path`` is the subscription-gate placeholder clip."""
    return _SPONSOR_MARKER in path.lower()


def _has_playable_files(groups: list[_PlaylistGroup]) -> bool:
    """True when the playlist exposes at least one file (a group-level
    ``file`` for movies, or an episode ``file`` inside a ``folder``).

    A playlist with non-empty groups but empty folders/no group.file —
    a third gated variant the audit #139 did not originally spec — is
    still "nothing playable", so this returns False for it."""
    if any(g.file for g in groups):
        return True
    return any(ep.file for g in groups for ep in g.folder)


def _require_playable_files(groups: list[_PlaylistGroup]) -> None:
    """Raise ``gated`` when a content page's playlist has no playable
    files at all.

    Sits between ``_require_playlist`` (catches the empty-groups shape:
    no ``const playlist`` block, or ``[]``) and ``_playlist_fully_gated``
    (catches the all-sponsor-placeholder shape). The third gated shape
    — non-empty groups whose every ``folder`` is empty and no group
    has a ``file`` — would otherwise fall through both guards:
    ``_require_playlist`` sees non-empty ``groups``, and
    ``_playlist_fully_gated`` returns ``False`` on the ``not files``
    branch. ``_build_seasons`` then returns ``None`` and content()
    surfaces a zero-season ``ContentResponse`` — the exact #139 break
    the gate is meant to prevent. Without this guard, an upstream
    variant like ``[{title:"Сезон 1",folder:[]}]`` would silently
    re-introduce it. Same verdict as ``_require_playlist``: deliberate
    upstream unavailability → ``gated`` (ADR-0002), so the can_gate
    catalog sweep drops the card during ``load_home`` instead of
    surfacing a zero-season series (#139)."""
    if not _has_playable_files(groups):
        raise ProviderError("gated", "playlist has no playable files")


def _playlist_fully_gated(groups: list[_PlaylistGroup]) -> bool:
    """True when EVERY playable file in the playlist is the gate placeholder.

    A series with some real episodes is NOT gated as a whole — its
    free episodes stay playable; only the placeholder ones are refused
    by ``stream()``. A playlist with no playable files at all is
    vacuously fully-gated — ``_require_playable_files`` raises before
    the caller reaches here, but the vacuous-True return keeps this
    helper correct if it is ever called without that pre-check."""
    files: list[str] = [g.file for g in groups if g.file]
    for g in groups:
        files.extend(ep.file for ep in g.folder)
    if not files:
        return True
    return all(_is_sponsor_file(f) for f in files)

# The upstream `playlistRegex` extracts the inline JSON manifest.
_PLAYLIST_RE = re.compile(r"const playlist\s*=\s*(\[.*?\]);", re.DOTALL)

# external_id is "<category>/<numeric-slug>" — single-segment
# ("cinema/1159-aichaku") or multi-segment for /zhanr/ cards
# ("zhanr/drama/1156-personasulli"). Gate content()/stream() against
# values that could escape the URL path; without this the caller could
# interpolate "../" segments upstream's http client would happily
# follow. Segments are restricted to lowercase letters + hyphens, so
# the only separators are literal slashes.
_SEGMENT = r"[a-z][a-z-]+"  # one path-segment prefix (min 2 chars)
_SLUG_RE = re.compile(rf"(?:{_SEGMENT}/)+\d+-[a-z0-9_-]+")


def _external_id_from_url(href: str) -> str:
    """Return an opaque id encoding the URL path. Content URLs have the
    form ``/kind/N-slug.html``; we keep the FULL path — every segment
    prefix, e.g. ``zhanr/drama/N-slug`` — so ``content()`` can rebuild
    ``f"{BASE_URL}/{external_id}.html"`` verbatim. Collapsing to the
    last two segments drops the category prefix and yields a URL the
    site 301-redirects (live 2026-08-08: ``/zhanr/drama/N-slug``
    collapses to ``drama/N-slug`` -> 301; only the full path is a 200)."""
    # Match the full URL — an optional scheme+host, then one or more
    # `segment/` prefixes + the numeric slug — and rebuild it verbatim,
    # regardless of any upstream category prefix. `re.match` anchors the
    # start so a bare relative href (`zhanr/drama/1156.html`, no leading
    # slash) is rejected instead of silently collapsing to `drama/...`.
    m = re.match(
        rf"(?:https?://[^/]+)?/((?:{_SEGMENT}/)+)(\d+-[a-z0-9_-]+?)(?:\.html)?/?$",
        href,
    )
    if not m:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return f"{m.group(1)}{m.group(2)}"


def _type_from_url(href: str) -> str:
    """Map the URL's path segment to a MediaType."""
    lower = href.lower()
    for needle, t in _PATH_TYPE:
        if f"/{needle}/" in lower:
            return t
    return "series"


def _page_number(href: str) -> int:
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _section_url(section: str, page: int) -> str:
    paths = {s.id: f"/{s.id}/" for s in BAMBOUA_SECTIONS}
    if section not in paths:
        raise ProviderError("not_found", f"unknown section: {section}")
    base = f"{BASE_URL}{paths[section]}"
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _parse_card(slide: Tag, provider_id: str) -> SearchResult | None:
    """Parse one cat-item slide. Featured banner-item slides lack the
    `h2.label-3` / `div.poster` markers and are filtered out by the
    caller's `div.cat-item` selector."""
    title_el = slide.select_one("h2.label-3")
    link = slide.select_one("a.link-title")
    if title_el is None or link is None or not link.get("href"):
        return None
    href = str(link["href"])
    img = slide.select_one("div.poster img")
    poster_src = str(img["src"]) if img and img.get("src") else None
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    try:
        ext = _external_id_from_url(href)
    except ProviderError:
        return None
    mb_form, mb_styles = model_b_axes(_type_from_url(href))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{ext}",
        provider=provider_id,
        title=title_el.get_text(strip=True),
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


# --- JSON DTOs mirroring BambooUAProvider.kt's JSONModel.kt -------------------------


class _Publisher(BaseModel):
    type: str | None = None
    name: str | None = None


class _MainEntity(BaseModel):
    type: str | None = None
    id: str | None = None


class _Author(BaseModel):
    type: str | None = None
    name: str | None = None
    url: str | None = None


class _GraphNode(BaseModel):
    type: str | None = None
    name: str | None = None
    headline: str | None = None
    description: str | None = None
    datePublished: str | None = None
    image: list[str] | None = None
    publisher: _Publisher | None = None
    mainEntityOfPage: _MainEntity | None = None
    author: _Author | None = None


class _JSONModel(BaseModel):
    """JSON-LD document model.

    The upstream emits standard JSON-LD keys (`@context`/`@graph`);
    without aliases pydantic ignored them and `graph` stayed [] — every
    bambooua detail rendered a blank description (Ticket #226)."""

    model_config = ConfigDict(populate_by_name=True)

    context: str | None = Field(default=None, alias="@context")
    graph: list[_GraphNode] = Field(default_factory=list, alias="@graph")


class _PlaylistEpisode(BaseModel):
    title: str = ""
    file: str


class _PlaylistGroup(BaseModel):
    title: str = ""
    folder: list[_PlaylistEpisode] = []
    # Some movies put the playable URL at the group level (instead of
    # inside a folder); the upstream Kotlin PlaylistGroup data class
    # only has `title`/`folder`, so those entries get dropped. We
    # capture the loose `file` field so movies still play.
    file: str | None = None


def _extract_playlist(html: str) -> list[_PlaylistGroup]:
    """Pull the `const playlist = [...]` array out of the page HTML.

    Mirrors the upstream `playlistRegex` extraction. Returns ``[]`` only
    when the page has NO ``const playlist`` block — an empty array block
    (``[]``) is returned as-is. A block that exists but won't parse
    (invalid JSON, or not an array) raises ``parse_failed``: that is a
    genuine parse gap (upstream shape change) which the health tracker
    must see, not something to swallow into an empty-season 200 (#139)."""
    m = _PLAYLIST_RE.search(html)
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", "playlist json invalid") from e
    if not isinstance(raw, list):
        raise ProviderError("parse_failed", "playlist not a list")
    out: list[_PlaylistGroup] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        groups = _PlaylistGroup.model_validate(item)
        out.append(groups)
    return out


def _require_playlist(groups: list[_PlaylistGroup]) -> None:
    """Raise ``gated`` when a content page carries no playable manifest.

    After ``_extract_playlist`` (which raises ``parse_failed`` on a
    malformed block), an empty result means the page genuinely has no
    manifest: the site serves subscription-gated titles («Для
    підписників») to non-subscribers with an empty/missing
    ``const playlist`` (live 2026-08-08: dorama/262-legenda-pro-nok-tu,
    lakorn/1035-khemjira, tv-show/652-his-man-season-1). Deliberate
    upstream unavailability → ``gated`` (ADR-0002), so the can_gate
    catalog sweep drops the card during ``load_home`` instead of
    surfacing a zero-season series (#139)."""
    if not groups:
        raise ProviderError("gated", "no playlist on content page")


def _parse_jsonld(html: str) -> _JSONModel | None:
    """Find the first `<script type="application/ld+json">` block and
    parse it as ``_JSONModel``. The upstream uses Gson to deserialize
    the full graph; we only need the first entity's name/description."""
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    ):
        try:
            return _JSONModel.model_validate(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# --- Provider -----------------------------------------------------------------------


class BambooUAProvider(BaseProvider):
    id = "bambooua"
    name = "BambooUA"
    types = ("movie", "series", "anime", "dorama")
    sections = BAMBOUA_SECTIONS
    #: Gated titles resolve to a sponsor promo clip; the catalog build
    #: drops those sources before they surface as cards.
    can_gate = True

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # The upstream POSTs to the bare mainUrl with the DLE search
        # fields. We use the same fields so the server-side response
        # shape matches what we captured.
        try:
            resp = await http.post(
                BASE_URL,
                data={
                    "do": "search",
                    "subaction": "search",
                    "story": quote(query),
                },
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for slide in soup.select("article.swiper-slide"):
            # The featured banner-item slides lack the `div.cat-item`
            # wrapper; skip them.
            if not slide.select_one("div.cat-item"):
                continue
            parsed = _parse_card(slide, self.id)
            if parsed is not None:
                results.append(parsed)
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        url = _section_url(section, page)
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for slide in soup.select("article.swiper-slide"):
            if not slide.select_one("div.cat-item"):
                continue
            parsed = _parse_card(slide, self.id)
            if parsed is not None:
                results.append(parsed)
        # BambooUA's pagination is `<div class="navigation">` with
        # `<a href=".../page/N/">` siblings. Any link to a higher page
        # than `page` means there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("div.navigation a[href*='/page/']")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        url = f"{BASE_URL}/{external_id}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        meta = _parse_jsonld(resp.text)
        # The first graph node carries the title + description for the
        # content itself; the second is usually a BreadcrumbList.
        title = (meta.graph[0].name or meta.graph[0].headline) if meta and meta.graph else None
        if not title:
            h1 = soup.select_one("h1")
            if h1 is not None:
                title = h1.get_text(strip=True)
        if not title:
            raise ProviderError("parse_failed", "title missing")
        og = soup.select_one('meta[property="og:image"]')
        poster = urljoin(BASE_URL, str(og["content"])) if og and og.get("content") else None
        description = (
            (meta.graph[0].description or "") if meta and meta.graph else ""
        )
        year_int: int | None = None
        if meta and meta.graph and meta.graph[0].datePublished:
            yyyy = meta.graph[0].datePublished[:4]
            if yyyy.isdigit():
                year_int = int(yyyy)
        country: str | None = extract_country(soup)
        groups = _extract_playlist(resp.text)
        _require_playlist(groups)
        _require_playable_files(groups)
        if _playlist_fully_gated(groups):
            raise ProviderError("gated", "subscription required")
        media_type = _type_from_url(url)
        seasons = self._build_seasons(groups, external_id, media_type, self.id)
        mb_form, mb_styles = model_b_axes(media_type)  # type: ignore[arg-type]
        return ContentResponse(
            id=f"bambooua:{external_id}",
            title=title.strip(),
            description=description,
            year=year_int,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            form=mb_form,
            styles=mb_styles,
            country=country,
        )

    @staticmethod
    def _build_seasons(
        groups: list[_PlaylistGroup], external_id: str, media_type: str, provider_id: str
    ) -> list[Season] | None:
        """Convert the upstream playlist into our `Season[]`.

        The upstream maps each `PlaylistGroup` based on its title:
        `Озвучення` / `Дубляж` -> Dubbed, `Субтитри` -> Subbed. Each
        group becomes a season, with episodes numbered from 1.
        """
        # Single-file movies collapse into season 1 with one episode.
        if media_type == "movie" and len(groups) == 1 and len(groups[0].folder) == 1:
            return [
                Season(
                    number=1,
                    episodes=[
                        Episode(
                            number=1,
                            id=f"{provider_id}:{external_id}{MOVIE_SUFFIX}",
                            title=groups[0].folder[0].title,
                        )
                    ],
                )
            ]
        seasons: list[Season] = []
        for s_idx, group in enumerate(groups, start=1):
            episodes = [
                Episode(
                    number=e_idx,
                    id=f"{provider_id}:{external_id}:s{s_idx}e{e_idx}",
                    title=ep.title,
                )
                for e_idx, ep in enumerate(group.folder, start=1)
            ]
            if not episodes:
                continue
            seasons.append(Season(number=s_idx, episodes=episodes))
        return seasons or None

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` arrives as either "<external_id>__movie__"
        # (movie shortcut) or "<external_id>:s<N>e<M>" (series episode).
        # `/api/stream` strips the `<provider>:` prefix before calling us.
        if MOVIE_SUFFIX in content_id:
            ext_id = content_id.split(MOVIE_SUFFIX, 1)[0]
            ep_suffix = ""
        elif ":" in content_id:
            ext_id, _, ep_suffix = content_id.rpartition(":")
        else:
            ext_id, ep_suffix = content_id, ""
        if not _SLUG_RE.fullmatch(ext_id):
            raise ProviderError("not_found", "bad external_id")
        url = f"{BASE_URL}/{ext_id}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        groups = _extract_playlist(resp.text)
        _require_playlist(groups)
        _require_playable_files(groups)
        media_url = self._select_file(groups, ep_suffix)
        if media_url is None:
            raise ProviderError("not_found", f"no file for {ep_suffix!r}")
        if _is_sponsor_file(media_url):
            raise ProviderError("gated", "subscription required")
        # Live titles are HLS (hlsN.bambooua.com/…/index.m3u8) or plain
        # mp4; label the stream by its actual URL, not a fixed "mp4".
        stream_type: StreamType = "m3u8" if media_url.lower().endswith(".m3u8") else "mp4"
        return StreamResponse(
            url=urljoin(BASE_URL, media_url),
            type=stream_type,
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/1.0"},
        )

    @staticmethod
    def _select_file(groups: list[_PlaylistGroup], ep_suffix: str) -> str | None:
        """Resolve the playlist to a single playable URL.

        Returns None when the suffix is malformed or out of range so the
        caller can surface an explicit `not_found`. There is no silent
        "first available episode" fallback — that would mask a missing
        suffix in the caller.
        """
        if not ep_suffix:
            # Movie: use the first group's first file. Movies on the
            # upstream site put the file at the group level rather than
            # inside a folder, so fall back to that.
            if groups:
                if groups[0].folder:
                    return groups[0].folder[0].file
                if groups[0].file:
                    return groups[0].file
            return None
        m = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
        if not m:
            return None
        s_idx, e_idx = int(m.group(1)), int(m.group(2))
        if not (1 <= s_idx <= len(groups)):
            return None
        folder = groups[s_idx - 1].folder
        if not (1 <= e_idx <= len(folder)):
            return None
        return folder[e_idx - 1].file


__all__ = ["BambooUAProvider"]
