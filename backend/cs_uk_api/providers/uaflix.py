"""UAFlix provider (https://uaflix.org) — Ukrainian-dubbed films,
serials, doramas, cartoons (Мультфільми), mult-serials and anime.
Issue #17, Group 1.

The live mirror captured in the fixtures is `uafix.net` (a sibling
of `uaflix.org`); both share the same template, so we hard-code
the captured domain to keep `respx` route matching deterministic.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from ..extractors import RegexExtractor
from ..country import extract_country
from ..http_client import safe_get
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, ProviderError, model_b_axes

# Captured-fixture domain. The live site mirrors `uaflix.org` to
# `uafix.net`; the markup is identical, so routing the provider
# against the captured URL keeps test fixtures in sync with
# production behavior.
BASE_URL = "https://uafix.net"
# Hosts the upstream may legally redirect to: the content page on
# uafix.net and the PlayerJS iframe on zetvideo.net. A hostile CMS
# response must not be able to pivot either hop elsewhere.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"uafix.net", "zetvideo.net"})

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
    Section(id="filmy", title="Фільми", type="movie"),
    Section(id="serialy", title="Серіали", type="series"),
    Section(id="doramy", title="Дорами", type="dorama"),
    Section(id="cartoons", title="Мультфільми", type="cartoon"),
    Section(id="multserialy", title="Мультсеріали", type="series"),
    Section(id="anime", title="Аніме", type="anime"),
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
    """
    section, _, slug = external_id.partition("-")
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
        type=_type_from_url(href),  # type: ignore[arg-type]
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
        type=_type_from_url(href),  # type: ignore[arg-type]
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


def _parse_seasons(soup: BeautifulSoup, external_id: str) -> list[Season]:
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
                id=f"{external_id}:s{s}e{e}",
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
        seasons.append(Season(number=s, episodes=eps))
    return seasons


class UAFlixProvider(BaseProvider):
    id = "uaflix"
    name = "UAFlix"
    types = ("movie", "series", "anime", "dorama", "cartoon")
    sections = UAFLIX_SECTIONS

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
        country: str | None = extract_country(soup)
        media_type = _type_from_url(url)
        seasons: list[Season] | None = None
        if media_type in ("series", "anime", "dorama"):
            seasons = _parse_seasons(soup, external_id)
        mb_form, mb_styles = model_b_axes(media_type)  # type: ignore[arg-type]
        return ContentResponse(
            id=f"uaflix:{external_id}",
            type=media_type,  # type: ignore[arg-type]
            title=title,
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

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
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        player_url = _extract_player_iframe(soup)
        if player_url is None:
            raise ProviderError(
                "parse_failed", "no player iframe found on content page"
            )
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
            raise ProviderError(
                "parse_failed", "no media URL found in player page"
            )
        return StreamResponse(
            url=extracted.url,
            type=extracted.type,
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/0.1"},
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
