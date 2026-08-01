"""BambooUA provider (https://bambooua.com) — Ukrainian-dubbed anime,
doramas, lakorns, TV-shows, cinema and LGBTQ BL. Issue #17, Group 1.

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
from pydantic import BaseModel

from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, ProviderError

BASE_URL = "https://bambooua.com"

# The upstream `mainPage = mainPageOf(...)` declares nine sections.
BAMBOUA_SECTIONS: tuple[Section, ...] = (
    Section(id="cinema", title="Фільми", type="movie"),
    Section(id="dorama", title="Дорами", type="series"),
    Section(id="anime", title="Аніме", type="anime"),
    Section(id="lakorn", title="Лакорн", type="series"),
    Section(id="voice", title="Озвучення", type="series"),
    Section(id="tv-show", title="ТВ-шоу", type="series"),
    Section(id="done", title="Завершені", type="series"),
    Section(id="world-bl", title="Світ ЛГБТ", type="series"),
    Section(id="now", title="Поточні", type="series"),
)

# URL path segment -> MediaType. Longest prefixes first so `world-bl`
# beats `world`, and `now` (single-segment) wins over any future
# longer prefix. The upstream maps: dorama -> AsianDrama, anime -> Anime,
# else -> Movie; but here we classify per-card so a `/cinema/` URL is
# movie and a `/dorama/` URL is series/dorama.
_PATH_TYPE: tuple[tuple[str, str], ...] = (
    ("world-bl", "series"),
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

# The upstream `playlistRegex` extracts the inline JSON manifest.
_PLAYLIST_RE = re.compile(r"const playlist\s*=\s*(\[.*?\]);", re.DOTALL)


def _external_id_from_url(href: str) -> str:
    """Return an opaque id encoding the URL path. Most content URLs
    have the form ``/kind/N-slug.html``; we collapse that to
    ``kind/N-slug``. Multi-segment paths (e.g. ``/zhanr/romantyka/N-slug``)
    keep only the last two segments so ``content()`` can rebuild the
    URL verbatim."""
    # Match the last two segments (the kind + slug) so the URL can be
    # rebuilt with `f"{BASE_URL}/{external_id}.html"` regardless of any
    # upstream category prefix.
    m = re.search(r"/([a-z][a-z-]*?)/(\d+-[a-z0-9_-]+?)(?:\.html)?/?$", href)
    if not m:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return f"{m.group(1)}/{m.group(2)}"


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
    return SearchResult(
        id=f"{provider_id}:{ext}",
        provider=provider_id,
        type=_type_from_url(href),  # type: ignore[arg-type]
        title=title_el.get_text(strip=True),
        poster=poster,
        url=urljoin(BASE_URL, href),
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
    image: list[str] | None = None
    publisher: _Publisher | None = None
    mainEntityOfPage: _MainEntity | None = None
    author: _Author | None = None


class _JSONModel(BaseModel):
    context: str | None = None
    graph: list[_GraphNode] = []


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
    Mirrors the upstream `playlistRegex` extraction."""
    m = _PLAYLIST_RE.search(html)
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[_PlaylistGroup] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        groups = _PlaylistGroup.model_validate(item)
        out.append(groups)
    return out


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
        groups = _extract_playlist(resp.text)
        media_type = _type_from_url(url)
        seasons: list[Season] | None = None
        if groups:
            seasons = self._build_seasons(groups, external_id, media_type)
        return ContentResponse(
            id=f"bambooua:{external_id}",
            type=media_type,  # type: ignore[arg-type]
            title=title.strip(),
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
        )

    @staticmethod
    def _build_seasons(
        groups: list[_PlaylistGroup], external_id: str, media_type: str
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
                            id=f"{external_id}{MOVIE_SUFFIX}",
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
                    id=f"{external_id}:s{s_idx}e{e_idx}",
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
        url = f"{BASE_URL}/{ext_id}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        groups = _extract_playlist(resp.text)
        if not groups:
            raise ProviderError("parse_failed", "no playlist on content page")
        media_url = self._select_file(groups, ep_suffix)
        if media_url is None:
            raise ProviderError("not_found", f"no file for {ep_suffix!r}")
        return StreamResponse(
            url=urljoin(BASE_URL, media_url),
            type="mp4",
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/0.1"},
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
