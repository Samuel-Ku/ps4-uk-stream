"""UAFlix provider (https://uaflix.org) — Ukrainian-dubbed films,
serials, doramas, cartoons (Мультфільми), mult-serials and anime.
Issue #17, Group 1.

The live mirror captured in the fixtures is `uafix.net` (a sibling
of `uaflix.org`); both share the same template, so we hard-code
the captured domain to keep `respx` route matching deterministic.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from ..country import extract_country
from ..extractors import ExtractResult, RegexExtractor
from ..http_client import safe_get
from ..models import (
    ContentResponse,
    Episode,
    Person,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, ProviderError, model_b_axes


def _itemprop_values(soup: BeautifulSoup, name: str) -> list[str]:
    """All values for a schema.org ``itemprop`` on the content page.

    The template mixes two shapes: `<meta itemprop=... content="…">`
    (serials) and `<span itemprop=...>…</span>` (movies). Returns
    stripped non-empty values in document order.
    """
    out: list[str] = []
    for el in soup.select(f'[itemprop="{name}"]'):
        value = str(el.get("content") or "").strip()
        if not value:
            value = el.get_text(" ", strip=True)
        if value:
            out.append(value)
    return out


def _parse_itemprop_meta(soup: BeautifulSoup) -> tuple[int | None, list[str], list[Person]]:
    """Year / genres / people from the page's schema.org itemprop
    metadata (ticket #228).

    The parser ignored the block entirely — every uaflix detail showed
    no year, no genres and an empty People rail while the page carried
    ``dateCreated`` / ``genre`` / ``actor`` / ``director``. People ids
    follow the ``uaflix:<role>:<name>`` convention so /Persons/{id}
    round-trips.
    """
    year: int | None = None
    for raw in _itemprop_values(soup, "dateCreated"):
        m = re.search(r"(19\d{2}|20\d{2})", raw)
        if m:
            year = int(m.group(1))
            break
    genres = [g for g in _itemprop_values(soup, "genre") if g]
    people: list[Person] = []
    for role, label in (("director", "Director"), ("actor", "Actor")):
        for name in _itemprop_values(soup, role):
            # A serial's director meta is a comma-joined string; a
            # movie's span is a single name (spaces are part of it).
            for part in re.split(r"\s*,\s*", name):
                part = part.strip()
                if part:
                    people.append(
                        Person(id=f"uaflix:{role}:{part}", name=part, role=label)
                    )
    return year, genres, people


# Captured-fixture domain. The live site mirrors `uaflix.org` to
# `uafix.net`; the markup is identical, so routing the provider
# against the captured URL keeps test fixtures in sync with
# production behavior.
BASE_URL = "https://uafix.net"
# Hosts the upstream may legally redirect to: the content page on
# uafix.net and the PlayerJS iframes on zetvideo.net / ashdi.vip. A
# hostile CMS response must not be able to pivot either hop elsewhere.
_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"uafix.net", "zetvideo.net", "ashdi.vip"}
)

# External-id boundary: `<section>-<slug>` (e.g. `serials-djuna-proroctvo`)
# where both halves are lowercase ASCII with hyphens. Anything else —
# path traversal, scheme injection — must surface as `not_found` before
# content()/stream() build a URL with it.
_EXTERNAL_ID_RE = re.compile(r"[a-z][a-z0-9-]*")

# Six sections per the upstream Kotlin `mainPage`. The captured site
# does not host `/multserialy/` — mult-serials are served from the
# sub-path `/serials/multseial/`. The `multserialy` section id stays
# as upstream Kotlin names it; only the URL path is aliased.
UAFLIX_SECTIONS: tuple[Section, ...] = (
    Section(id="filmy", title="Фільми", form="movie"),
    Section(id="serialy", title="Серіали", form="series"),
    Section(id="doramy", title="Дорами", styles=frozenset({"dorama"})),
    Section(id="cartoons", title="Мультфільми", styles=frozenset({"cartoon"})),
    Section(id="multserialy", title="Мультсеріали", form="series"),
    Section(id="anime", title="Аніме", styles=frozenset({"anime"})),
)

# Map section id -> browse URL path. Five sections use the natural
# root; multserialy aliases to the live mirror's sub-path.
_SECTION_PATHS: dict[str, str] = {
    "filmy": "/films/",
    "serialy": "/serials/",
    "doramy": "/dorama/",
    "cartoons": "/cartoons/",
    "multserialy": "/serials/multseial/",
    "anime": "/anime/",
}

# Path prefix -> MediaType. Longest prefix first so `/films/` matches
# `film` (not anything shorter), and `/serials/` matches `series`
# (not `serial`). Mirrors the upstream Kotlin's `when` ordering.
_PATH_TYPE: tuple[tuple[str, str], ...] = (
    ("cartoon", "cartoon"),  # /cartoons/
    ("serial", "series"),    # /serials/, /serials/multseial/
    ("anime", "anime"),      # /anime/
    ("dorama", "dorama"),    # /dorama/
    ("film", "movie"),       # /films/
)


def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _external_id_from_url(href: str) -> str:
    """Encode a content URL as `<section>-<slug>`.

    The live site uses `<section>/<slug>/` (no `.html`, no numeric id);
    the upstream Kotlin's `<section_kind>-<numeric_id>-<slug>` shape
    does not match the captured markup, so we go with what the live
    site actually emits.
    """
    m = re.search(r"/([a-z][a-z0-9-]*?)/([a-z0-9][a-z0-9-]*?)(?:/|$|\.html)", href)
    if not m:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return f"{m.group(1)}-{m.group(2)}"


def _type_from_url(href: str) -> str:
    """Map the URL's first path segment to a MediaType."""
    lower = href.lower()
    for needle, t in _PATH_TYPE:
        if f"/{needle}" in lower:
            return t
    return "series"  # safe default


def _section_url(section: str, page: int) -> str:
    path = _SECTION_PATHS.get(section)
    if path is None:
        raise ProviderError("not_found", f"unknown section: {section}")
    base = f"{BASE_URL}{path}"
    # Page 1 is the index; subsequent pages use `/page/N/`.
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _content_url(external_id: str) -> str:
    """Reverse `_external_id_from_url` for the bare external_id.

    Used by `stream()` to rebuild the content URL from an id. The
    id's first segment is the section; the rest is the slug.

    The upstream emits TWO URL shapes (live 2026-08-09): older
    plain-slug pages answer at `/section/<slug>/` (trailing slash),
    while newer numeric-id pages answer ONLY at `/section/<id>-<slug>`
    + ``.html`` (e.g. `/anime/100067-geroinja-zi-strichkoju.html`);
    the trailing-slash form 404s for them. The slug's leading digit
    is the discriminator.
    """
    section, _, slug = external_id.partition("-")
    if slug and slug[0].isdigit():
        return f"{BASE_URL}/{section}/{slug}.html"
    return f"{BASE_URL}/{section}/{slug}/"


def _episode_content_url(external_id: str, ep_suffix: str) -> str:
    """Build a per-episode content URL like
    `.../serials/djuna-proroctvo/season-01-episode-01/`.

    `ep_suffix` is the `s<N>e<M>` encoding from the seasons list
    (e.g. `s1e1` -> `season-01-episode-01`).
    """
    section, _, slug = external_id.partition("-")
    m = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
    if not m:
        raise ProviderError("parse_failed", f"bad episode suffix: {ep_suffix!r}")
    season_num = int(m.group(1))
    episode_num = int(m.group(2))
    return (
        f"{BASE_URL}/{section}/{slug}/"
        f"season-{season_num:02d}-episode-{episode_num:02d}/"
    )


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one listing card (`<a class="vi-img img-resp-h">`).

    Cards on the listing pages wrap the entire tile in the anchor,
    so we read the title from `<div class="vi-title">` inside the
    anchor itself and the poster from the `<img>` either via `src`
    or `data-src` (the markup uses both: the visible `src` is a
    lazy-poster placeholder and the real URL is in `data-src`).
    """
    # Card root: on listings, the anchor is itself the card. On search
    # results, the card wraps multiple inner elements. Caller decides.
    a = card.select_one("a.vi-img") if card.name != "a" else card
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    title_el = card.select_one(".vi-title")
    if title_el is None:
        return None
    title = title_el.get_text(" ", strip=True)
    if not title:
        return None
    img = card.select_one("img")
    poster_src: str | None = None
    if img is not None:
        for attr in ("data-src", "src"):
            v = img.get(attr)
            if isinstance(v, str) and not v.endswith("lazy-poster.png"):
                poster_src = v
                break
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    try:
        external_id = _external_id_from_url(href)
    except ProviderError:
        return None
    mb_form, mb_styles = model_b_axes(_type_from_url(href))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


def _parse_search_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one `<a class="sres-wrap clearfix">` search result.

    Search results are flat: the anchor wraps an `<img>`, an `<h2>`
    title and a description block. The poster is the image's `src`.
    """
    if card.name != "a" or not card.get("href"):
        return None
    href = str(card["href"])
    title_el = card.select_one("h2")
    if title_el is None:
        return None
    title = title_el.get_text(" ", strip=True)
    if not title:
        return None
    img = card.select_one(".sres-img img")
    poster_src = str(img["src"]) if img and img.get("src") else None
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    try:
        external_id = _external_id_from_url(href)
    except ProviderError:
        return None
    mb_form, mb_styles = model_b_axes(_type_from_url(href))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


def _extract_player_iframe(soup: BeautifulSoup) -> str | None:
    """Pull the player URL out of the content page's `<iframe src=...>`.

    The content page embeds the player inside
    `<div class="tabs-b video-box"><iframe src="...">` (also exposed
    as `<meta property="og:video:iframe">`). Both routes give the same
    URL; we prefer the iframe so a future template change that drops
    the meta tag does not break us.
    """
    iframe = soup.select_one(".tabs-b.video-box iframe")
    if iframe is None:
        iframe = soup.select_one("div.fplayer iframe")
    if iframe is None:
        return None
    src = iframe.get("src")
    return str(src) if src else None


def _file_value(html: str) -> str | None:
    """Raw PlayerJS `file:` payload (either quoting style).

    The generic `RegexExtractor` only matches a direct media URL, but
    serial player pages instead store the episode URLs inside a nested
    JSON-folder string (`file:'[{"folder":[{"folder":[{"file":"<url>"}]}]}]'`).
    Pull the raw value first so the caller can decide which shape it is.
    """
    m = re.search(r"file\s*:\s*(?:\"([^\"]+)\"|'([^']+)')", html)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _is_youtube_player(url: str) -> bool:
    """True when the player iframe is a YouTube embed (trailer-only)."""
    return "youtube.com" in url or "youtu.be" in url


def _is_serial_player(url: str) -> bool:
    """True when the player iframe is a zetvideo serial player."""
    return "/serial/" in url


def _serial_media_url(html: str, ep_suffix: str) -> str | None:
    """Resolve an episode m3u8 from a serial player's JSON-folder `file:`.

    Serial pages nest episodes as
    `file:'[{"folder":[{"folder":[{"file":"<url>",...}]}]}]'` — outer
    list is the dubbing/track, then seasons, then episodes. `ep_suffix`
    (`s<N>e<M>`, e.g. `s1e1`) indexes season N / episode M exactly like
    the eneyida reference. Returns None when the page is not a
    JSON-folder shape, the suffix does not match, or the episode is
    missing.
    """
    raw = _file_value(html)
    if raw is None:
        return None
    m = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
    if not m:
        return None
    season = int(m.group(1))
    episode = int(m.group(2))
    try:
        data = json.loads(raw)
        value = data[0]["folder"][season - 1]["folder"][episode - 1]["file"]
        return value if isinstance(value, str) else None
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None


def _parse_poster(soup: BeautifulSoup) -> str | None:
    """Resolve the poster URL from the content page.

    The `<img class="lazy ...">` markup uses `src=` as a placeholder
    and the real URL lives in `data-src=`. Prefer that; fall back to
    the `<meta property="og:image">` tag if the img selectors fail.
    """
    img = soup.select_one(".fposter2 img")
    if img is not None:
        data_src = img.get("data-src")
        if isinstance(data_src, str) and data_src:
            return urljoin(BASE_URL, data_src)
        src = img.get("src")
        if isinstance(src, str) and src and "lazy-poster" not in src:
            return urljoin(BASE_URL, src)
    for meta in soup.select('meta[property="og:image"]'):
        content = meta.get("content")
        if isinstance(content, str) and content:
            return urljoin(BASE_URL, content)
    return None


def _parse_seasons(
    soup: BeautifulSoup, external_id: str, provider_id: str
) -> list[Season]:
    """Extract `(season -> [Episode])` from the content page.

    The series content page lists episodes inside `.frels2 .video-item`
    tiles, each pointing to `<section>/<show-slug>/season-NN-episode-NN/`.
    It also exposes a `.fusers.all-sez .sect-link` block with one link
    per season (`/sezon-N/`). We use both signals:

    1. seasons from `.fusers.all-sez a[href*="sezon-"]`
    2. episodes from `.video-item a.vi-img[href*="season-"]`

    Episodes are sorted by canonical season+episode URL position so
    `Episode.number` matches the on-page episode index (the site
    emits cards in reverse-chronological order, so document order
    would otherwise invert the numbering).
    """
    season_hrefs: set[int] = set()
    for a in soup.select(".fusers.all-sez a[href*='sezon-']"):
        m = re.search(r"sezon-(\d+)/?", str(a.get("href") or ""))
        if m:
            season_hrefs.add(int(m.group(1)))
    # Collect raw (season, episode) tuples from episode links, then
    # sort by (season, episode) to assign canonical Episode.number.
    raw_episodes: list[tuple[int, int, str]] = []
    for a in soup.select(".video-item a.vi-img"):
        href = str(a.get("href") or "")
        m = re.search(r"season-(\d+)-episode-(\d+)/?", href)
        if not m:
            continue
        s, e = int(m.group(1)), int(m.group(2))
        title_el = a.select_one(".vi-title")
        title = title_el.get_text(" ", strip=True) if title_el else f"Серія {e}"
        raw_episodes.append((s, e, title))
    raw_episodes.sort(key=lambda t: (t[0], t[1]))
    episodes_by_season: dict[int, list[Episode]] = {}
    for s, e, title in raw_episodes:
        episodes_by_season.setdefault(s, []).append(
            Episode(
                number=len(episodes_by_season[s]) + 1,
                id=f"{provider_id}:{external_id}:s{s}e{e}",
                title=title,
            )
        )
    # If the season block was missing, fall back to "every episode is
    # season 1" so callers still see a non-empty list.
    if not season_hrefs:
        season_hrefs = set(episodes_by_season.keys()) or {1}
    seasons: list[Season] = []
    for s in sorted(season_hrefs):
        eps = episodes_by_season.get(s, [])
        # Drop empty seasons. The upstream content page only renders
        # the latest seasons' episode tiles inline (older seasons' links
        # point at a separate `/sezon-N/` page), so an empty season has
        # NO episode ids — a client picking it (e.g. a play sweep that
        # starts at seasons[0]) lands on the main page, finds no player
        # iframe, and fails with a dead stream. Advertising only
        # seasons we can actually play keeps the card healthy.
        if not eps:
            continue
        seasons.append(Season(number=s, episodes=eps))
    return seasons


class UAFlixProvider(BaseProvider):
    id = "uaflix"
    name = "UAFlix"
    types = ("movie", "series", "anime", "dorama", "cartoon")
    sections = UAFLIX_SECTIONS
    #: Issue #189: trailer-only content pages (a YouTube embed with no
    #: playable player) raise ``gated`` at content() time so the
    #: catalog sweep (ADR-0002) drops the dead card from home/search
    #: instead of failing only at play time.
    can_gate = True

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # DLE-style search: POST with `do`, `subaction`, `story` to the
        # CMS endpoint. `quote()` handles non-ASCII Cyrillic and
        # reserved characters; httpx then url-form-encodes the rest.
        url = f"{BASE_URL}/index.php?do=search"
        try:
            resp = await http.post(
                url,
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
        for card in soup.select("a.sres-wrap.clearfix"):
            parsed = _parse_search_card(card, self.id)
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
        # Listing cards: each `.video-item` wraps an inner `<a class="vi-img">`.
        # We select the inner anchor to avoid double-counting (the outer
        # `.video-item` div is itself counted by `select(".video-item")`).
        for anchor in soup.select(".video-item a.vi-img"):
            parsed = _parse_card(anchor, self.id)
            if parsed is not None:
                results.append(parsed)
        # has_next: DLE pagination is `<div class="navigation">` with
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
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        url = _content_url(external_id)
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one("h1#ftitle")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        # The h1 sometimes wraps the real title in `<span itemprop="name">`
        # and adds `дивитись онлайн` suffix. Prefer the span's text if
        # present so we keep just the movie / series name.
        name_el = title_el.select_one('[itemprop="name"]')
        title = (
            name_el.get_text(" ", strip=True) if name_el else title_el.get_text(" ", strip=True)
        )
        poster = _parse_poster(soup)
        desc_el = soup.select_one("#serial-kratko, .fdesc.full-text")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""
        year_int, genres, people = _parse_itemprop_meta(soup)
        country: str | None = extract_country(soup)
        media_type = _type_from_url(url)
        seasons: list[Season] | None = None
        if media_type in ("series", "anime", "dorama", "cartoon"):
            seasons = _parse_seasons(soup, external_id, self.id)
            if not seasons:
                # Issue #189: some serials (e.g. «Вайлд Пак») expose
                # their episodes ONLY inside the serial player's
                # JSON-folder payload — the content page has no
                # season/episode links at all. Probe the player iframe
                # for the structure so the card gets playable ids.
                seasons = await self._serial_player_seasons(
                    soup, external_id, http
                )
        if not seasons:
            # Issue #189: a content page with no playable player at all
            # (YouTube-only embed or nothing) is a dead card — gate it
            # (ADR-0002) so the catalog sweep drops it from home/search
            # instead of failing only at play time. A VOD player
            # (zetvideo.net/vod, ashdi.vip/vod) is a movie-style single
            # stream that stream() resolves with the bare id, so the
            # card stays; a serial player whose JSON-folder probe came
            # up empty is unplayable and gates too.
            player_url = _extract_player_iframe(soup)
            if (
                player_url is None
                or _is_youtube_player(player_url)
                or _is_serial_player(player_url)
            ):
                raise ProviderError(
                    "gated", "trailer only — no playable player"
                )
        mb_form, mb_styles = model_b_axes(media_type)  # type: ignore[arg-type]
        return ContentResponse(
            id=f"uaflix:{external_id}",
            title=title,
            description=description,
            year=year_int,
            poster=poster,
            genres=genres,
            people=people,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

    async def _serial_player_seasons(
        self,
        soup: BeautifulSoup,
        external_id: str,
        http: httpx.AsyncClient,
    ) -> list[Season] | None:
        """Probe the serial player iframe for the episode structure.

        Some serials (e.g. «Вайлд Пак», observed live 2026-08-09) have
        NO episode links on the content page — the episodes live only
        inside the serial player's JSON-folder `file:` payload
        (`[{"folder":[{"folder":[{"file":...}]}]}]`). stream() already
        resolves `s<N>e<M>` suffixes against that payload
        (`_serial_media_url`), so content() must surface the same
        season/episode ids. Returns None when the player is not a
        serial, the fetch fails, or the payload is not the expected
        JSON-folder shape.
        """
        player_url = _extract_player_iframe(soup)
        if player_url is None or not _is_serial_player(player_url):
            return None
        player_url = urljoin(_content_url(external_id), player_url)
        try:
            resp = await safe_get(
                http,
                player_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers={"Referer": f"{BASE_URL}/"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        raw = _file_value(resp.text)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list) or not data:
            return None
        seasons: list[Season] = []
        for s_idx, season in enumerate(data[0].get("folder") or [], start=1):
            if not isinstance(season, dict):
                continue
            episodes = [
                Episode(
                    number=e_idx,
                    id=f"{self.id}:{external_id}:s{s_idx}e{e_idx}",
                    title=(
                        str(ep.get("title") or "").strip() or f"Серія {e_idx}"
                    ),
                )
                for e_idx, ep in enumerate(season.get("folder") or [], start=1)
            ]
            if episodes:
                seasons.append(Season(number=s_idx, episodes=episodes))
        return seasons or None

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # Two-hop resolution: content page -> player iframe (zetvideo.net)
        # -> m3u8 URL extracted from PlayerJS `file: "..."` config.
        # No JS execution needed (PlayerJS stores the URL inline).
        ext_id, ep_suffix = self._split_content_id(content_id)
        if not _EXTERNAL_ID_RE.fullmatch(ext_id):
            raise ProviderError("not_found", f"bad external_id: {ext_id!r}")
        content_url = (
            _episode_content_url(ext_id, ep_suffix)
            if ep_suffix
            else _content_url(ext_id)
        )
        try:
            resp = await safe_get(
                http,
                content_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers={"Referer": f"{BASE_URL}/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200 and ep_suffix:
            # Issue #189: serial-player-only titles (e.g. «Вайлд Пак»)
            # have NO per-episode pages — every season/episode lives
            # inside the show page's serial player JSON-folder. The
            # episode URL 404s, so fall back to the show page, which
            # still embeds the serial player we index by `s<N>e<M>`.
            content_url = _content_url(ext_id)
            try:
                resp = await safe_get(
                    http,
                    content_url,
                    allowed_hosts=set(_ALLOWED_HOSTS),
                    headers={"Referer": f"{BASE_URL}/"},
                )
            except httpx.HTTPError as e:
                raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        player_url = _extract_player_iframe(soup)
        if player_url is None:
            raise ProviderError(
                "parse_failed", "no player iframe found on content page"
            )
        # Upstream often emits a protocol-relative src (`//ashdi.vip/...`,
        # observed live 2026-08-09). The host check below still passes
        # (urlparse().netloc is populated) but httpx cannot fetch a
        # scheme-less URL, so resolve it against the content page first.
        player_url = urljoin(content_url, player_url)
        try:
            player_resp = await safe_get(
                http,
                player_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers={"Referer": f"{BASE_URL}/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError("not_found", f"status {player_resp.status_code}")
        extracted = RegexExtractor().extract(player_resp.text)
        if extracted is None or not extracted.url:
            # VOD players expose a direct m3u8 (handled above); serial
            # players expose a nested JSON-folder string the generic
            # extractor cannot see, so resolve the episode from the
            # `s<N>e<M>` suffix the same way the eneyida provider does.
            serial_url = _serial_media_url(player_resp.text, ep_suffix)
            if serial_url is None:
                raise ProviderError(
                    "parse_failed", "no media URL found in player page"
                )
            extracted = ExtractResult(url=serial_url, type="m3u8")
        return StreamResponse(
            url=extracted.url,
            type=extracted.type,
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/1.0"},
        )

    @staticmethod
    def _split_content_id(content_id: str) -> tuple[str, str]:
        """Split `content_id` into `(external_id, ep_suffix)`.

        `content_id` arrives from `/api/stream` as either
        `<external_id>` (movie) or `<external_id>:s<N>e<M>` (series
        episode). The colon after the leading `<section>-` is the
        boundary marker so hyphens in the slug never confuse us.
        """
        if ":" in content_id:
            ext_id, _, suffix = content_id.partition(":")
            return ext_id, suffix
        return content_id, ""


__all__ = ["UAFlixProvider"]
