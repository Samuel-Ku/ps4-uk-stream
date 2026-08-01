"""UFDub provider (https://ufdub.com) — Ukrainian-dubbed anime, films,
serials, doramas, cartoons, mult-serials. Issue #17, Group 1."""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from ..models import (
    ContentResponse,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, ProviderError

BASE_URL = "https://ufdub.com"

UFDUB_SECTIONS: tuple[Section, ...] = (
    Section(id="filmy", title="Фільми", type="movie"),
    Section(id="serialy", title="Серіали", type="series"),
    Section(id="doramy", title="Дорами", type="dorama"),
    Section(id="cartoons", title="Мультфільми", type="movie"),
    Section(id="multserialy", title="Мультсеріали", type="series"),
    Section(id="anime", title="Аніме", type="anime"),
)

# Path prefix -> MediaType. Order matters: longest prefixes first so
# `/cartoon-serial/` is classified as `series` (not `movie` via `cartoon`)
# and `/serial/` is not also matched by `/serials/`. Per the upstream
# Kotlin source's `when { contains("serials") -> TvType.TvSeries ... }`
# logic.
_PATH_TYPE: tuple[tuple[str, str], ...] = (
    ("cartoon-serial", "series"),  # /cartoon-serial/ (multserialy)
    ("serial", "series"),          # /serial/, /serials/
    ("cartoon", "movie"),          # /cartoon/, /cartoons/
    ("film", "movie"),             # /film/
    ("dorama", "dorama"),          # /dorama/
    ("anime", "anime"),            # /anime/
)


def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _external_id_from_url(href: str) -> str:
    """Return "kind-slug" where kind is film/serial/etc. (possibly with
    a hyphen, e.g. `cartoon-serial`) and slug is the numeric-prefixed
    part of the path (e.g. "48-fokus-pokus-hocus-pocus")."""
    m = re.search(r"/([a-z][a-z-]*?)/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href)
    if not m:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return f"{m.group(1)}-{m.group(2)}"


def _type_from_url(href: str) -> str:
    """Map the URL's path segment to a MediaType."""
    lower = href.lower()
    for needle, t in _PATH_TYPE:
        if f"/{needle}" in lower:
            return t
    return "series"  # safe default


def _section_url(section: str, page: int) -> str:
    paths = {
        "filmy": "/film/",
        "serialy": "/serial/",
        "doramy": "/dorama/",
        "cartoons": "/cartoon/",
        "multserialy": "/cartoon-serial/",
        "anime": "/anime/",
    }
    if section not in paths:
        raise ProviderError("not_found", f"unknown section: {section}")
    base = f"{BASE_URL}{paths[section]}"
    # Page 1 is the index; subsequent pages use `/page/N/`.
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _parse_card(card: Tag | BeautifulSoup, provider_id: str) -> SearchResult | None:
    """Parse one listing card (.short-t anchor)."""
    a = card.select_one("a.short-t")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    title = a.get_text(" ", strip=True)
    img = card.select_one(".img-box img")
    poster = urljoin(BASE_URL, str(img["src"])) if img and img.get("src") else None
    try:
        external_id = _external_id_from_url(href)
    except ProviderError:
        return None
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        type=_type_from_url(href),  # type: ignore[arg-type]
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
    )


class UFDubProvider(BaseProvider):
    id = "ufdub"
    name = "UFDub"
    types = ("movie", "series", "anime", "dorama")
    sections = UFDUB_SECTIONS

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        url = f"{BASE_URL}/index.php?do=search"
        try:
            # DLE (UFDub's CMS) accepts a POST with the same fields the
            # upstream Kotlin uses. `quote()` handles non-ASCII Cyrillic
            # and reserved characters; httpx then url-form-encodes the
            # rest.
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
        for card in soup.select(".short-t"):
            parsed = _parse_card(card.parent or card, self.id)
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
        # The upstream Kotlin removes `.section` (ad/featured blocks) before
        # mapping `.short` cards. Each card is `<div class="short clearfix">`
        # containing a `<div class="short-text">` with the title link. We
        # select only the inner wrapper to avoid double-counting.
        results: list[SearchResult] = []
        for card in soup.select(".short-text"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # has_next: DLE pagination is `<span class="navigation">` with
        # `<a href="/section/page/N/">` siblings. Any link to a higher page
        # than `page` means there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("span.navigation a[href*='/page/']")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        kind, _, slug = external_id.partition("-")
        url = f"{BASE_URL}/{kind}/{external_id[len(kind) + 1:]}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one("h1.top-title")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        poster_el = soup.select_one("div.f-poster img")
        poster = (
            urljoin(BASE_URL, str(poster_el["src"]))
            if poster_el and poster_el.get("src")
            else None
        )
        desc_el = soup.select_one("div.full-text p")
        description = desc_el.get_text(strip=True) if desc_el else ""
        # Player URL is in an <input value="..."> or an inline JS var.
        player_url = self._extract_player_url(soup)
        media_type = _type_from_url(url)
        seasons: list[Season] | None = None
        if media_type == "series" or media_type == "anime":
            seasons = self._parse_seasons(soup, player_url)
        return ContentResponse(
            id=f"ufdub:{external_id}",
            type=media_type,  # type: ignore[arg-type]
            title=title_el.get_text(strip=True),
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
        )

    @staticmethod
    def _extract_player_url(soup: BeautifulSoup) -> str | None:
        # Upstream: `input[value*=https://video.ufdub.com]` OR
        # `var input_player="...";` in an inline script.
        for inp in soup.select("input"):
            v = inp.get("value")
            if isinstance(v, str) and "video.ufdub.com" in v:
                return v
        for script in soup.select("script"):
            text = script.get_text()
            m = re.search(r'input_player\s*=\s*["\']([^"\']+)["\']', text)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _parse_seasons(soup: BeautifulSoup, player_url: str | None) -> list[Season] | None:
        # Upstream fetches the player page and extracts per-episode URLs
        # from a regex on the inline script. We defer that round-trip
        # for now and surface an empty season list (single season,
        # no episodes) when player_url is present. The live gate will
        # fill in episodes via /api/stream lookups.
        if player_url is None:
            return None
        return [Season(number=1, episodes=[])]

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # UFDub's player is a second-level page on `video.ufdub.com`. The
        # content page references it via `input_player=...`, and the real
        # media URL lives in that page's `var a = [['Серія 1','mp4', url]]`
        # array. Follow both hops (HTML + regex only, spec ground rule #4).
        kind, _, slug = content_id.partition("-")
        content_url = f"{BASE_URL}/{kind}/{content_id[len(kind) + 1:]}.html"
        try:
            resp = await http.get(
                content_url, headers={"Referer": f"{BASE_URL}/"}, follow_redirects=True
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        player_url = self._extract_player_url(soup)
        if player_url is None:
            raise ProviderError(
                "parse_failed", "no player iframe found on content page"
            )
        try:
            player_resp = await http.get(
                player_url, headers={"Referer": f"{BASE_URL}/"}, follow_redirects=True
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError("not_found", f"status {player_resp.status_code}")
        media_url = self._extract_media_url(player_resp.text)
        if media_url is None:
            raise ProviderError(
                "parse_failed", "no media URL found in player page"
            )
        return StreamResponse(
            url=media_url,
            type="mp4",
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/0.1"},
        )

    @staticmethod
    def _extract_media_url(player_html: str) -> str | None:
        scripts = BeautifulSoup(player_html, "lxml").select("script")
        text = next((item.get_text() for item in scripts if "var a=" in item.get_text()), "")
        match = re.search(r"\[[^\]]*,\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]", text)
        if not match:
            return None
        return match.group(1)


__all__ = ["UFDubProvider"]