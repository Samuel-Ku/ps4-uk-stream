from __future__ import annotations

import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from ..extractors import RegexExtractor
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

BASE_URL = "https://uakino.club"

# Sections exposed by Uakino's main navigation. The /animeukr URL is the
# anime sub-site (Ukrainian-dubbed anime). Section ids are stable; titles
# are user-facing and may change.
UAKINO_SECTIONS: tuple[Section, ...] = (
    Section(id="filmy", title="Фільми", type="movie"),
    Section(id="serials", title="Серіали", type="series"),
    Section(id="animeukr", title="Аніме", type="series"),
    Section(id="cartoons", title="Мультфільми", type="movie"),
)

# Pagination: how many cards per listing page. Uakino's default block is 12.
_PAGE_SIZE = 12


def _external_id_from_url(href: str) -> str:
    m = re.search(r"/(film|serial)/([\w-]+)", href)
    if not m:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return f"{m.group(1)}-{m.group(2)}"


def _is_series(href: str) -> bool:
    return "/serial/" in href


def _section_url(section: str, page: int) -> str:
    """Build the listing URL for a Uakino section + page.

    Anime is served from a different host; everything else uses the main
    site. Pagination on Uakino uses /page/N/.
    """
    if section == "animeukr":
        base = "https://animeukr.info"
        path = "/filmy"
    elif section == "filmy":
        base = BASE_URL
        path = "/filmy"
    elif section == "serials":
        base = BASE_URL
        path = "/serials"
    elif section == "cartoons":
        base = BASE_URL
        path = "/cartoons"
    else:
        raise ProviderError("not_found", f"unknown section: {section}")
    if page <= 1:
        return f"{base}{path}/"
    return f"{base}{path}/page/{page}/"


def _parse_listing(html: str, provider_id: str, default_type: str) -> tuple[list[SearchResult], bool]:
    """Parse a Uakino listing page.

    Returns (results, has_next). `has_next` is True if the pagination block
    contains a link to a higher page number, signalling more results.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []
    for card in soup.select("div.short-story"):
        a = card.select_one("h3.short-title a")
        img = card.select_one("div.short-img img")
        meta = card.select_one("div.short-meta")
        if a is None or a.get("href") is None or a.text is None:
            continue
        href = str(a["href"])
        title = a.get_text(strip=True)
        year_match = re.search(r"\b(19|20)\d{2}\b", title + " " + (meta.get_text() if meta else ""))
        year = int(year_match.group(0)) if year_match else None
        poster = urljoin(BASE_URL, str(img["src"])) if img and img.get("src") else None
        kind = "series" if _is_series(href) else default_type
        try:
            external_id = _external_id_from_url(href)
        except ProviderError:
            continue
        results.append(
            SearchResult(
                id=f"{provider_id}:{external_id}",
                provider=provider_id,
                type=kind,  # type: ignore[arg-type]
                title=title,
                year=year,
                poster=poster,
                url=urljoin(BASE_URL, href),
            )
        )
    # "has_next" detection: any link in the pagination block whose URL
    # contains a page number > current page.
    has_next = bool(soup.select("div.navigation a[href*='/page/']"))
    return results, has_next


class UakinoProvider(BaseProvider):
    id = "uakino"
    name = "Uakino"
    types = ("movie", "series")
    sections = UAKINO_SECTIONS

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        url = f"{BASE_URL}/search/?q={quote(query)}"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select("div.short-story"):
            a = card.select_one("h3.short-title a")
            img = card.select_one("div.short-img img")
            meta = card.select_one("div.short-meta")
            if a is None or a.get("href") is None or a.text is None:
                continue
            href = str(a["href"])
            title = a.get_text(strip=True)
            year_match = re.search(r"\b(19|20)\d{2}\b", title + " " + (meta.get_text() if meta else ""))
            year = int(year_match.group(0)) if year_match else None
            poster = urljoin(BASE_URL, str(img["src"])) if img and img.get("src") else None
            results.append(
                SearchResult(
                    id=f"uakino:{_external_id_from_url(href)}",
                    provider=self.id,
                    type="series" if _is_series(href) else "movie",
                    title=title,
                    year=year,
                    poster=poster,
                    url=urljoin(BASE_URL, href),
                )
            )
        return results

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        kind, _, slug = external_id.partition("-")
        # external_id looks like "film-dune-2021" or "serial-breaking-bad-s01"
        # Strip the leading kind so the rest of the slug matches the URL path.
        # If the slug already starts with the kind, leave it alone.
        url = f"{BASE_URL}/{kind}/{external_id[len(kind) + 1:]}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one("h1.hname")
        desc_el = soup.select_one("div.fulldesc")
        poster_el = soup.select_one("div.poster img")
        trans_el = soup.select_one("select#translations")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        translations = [
            Translation(id=str(opt.get("value")), label=opt.get_text(strip=True))
            for opt in (trans_el.select("option") if trans_el else [])
            if opt.get("value")
        ]
        if not translations:
            translations = [Translation(id="uk", label="Українська")]
        seasons: list[Season] | None = None
        kind_lower = kind.lower()
        if kind_lower == "serial":
            series_el = soup.select_one("#series-list")
            seasons = []
            if series_el and series_el.get("data-series"):
                import json
                raw = json.loads(str(series_el["data-series"]))
                for s in raw:
                    episodes = [
                        Episode(
                            number=i + 1,
                            id=f"uakino:{ep['id']}",
                            title=ep["title"],
                        )
                        for i, ep in enumerate(s["episodes"])
                    ]
                    seasons.append(Season(number=len(seasons) + 1, episodes=episodes))
        poster = urljoin(BASE_URL, str(poster_el["src"])) if poster_el and poster_el.get("src") else None
        return ContentResponse(
            id=f"uakino:{external_id}",
            type="series" if kind_lower == "serial" else "movie",
            title=title_el.get_text(strip=True),
            description=desc_el.get_text(strip=True) if desc_el else "",
            poster=poster,
            translations=translations,
            seasons=seasons,
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        player_url = f"{BASE_URL}/player/{content_id}.html"
        try:
            resp = await http.get(player_url, headers={"Referer": f"{BASE_URL}/"})
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        # Uakino embeds an iframe whose src is the actual stream. The
        # extracted URL is sometimes already a direct .m3u8, sometimes a
        # landing page that itself contains the media URL via PlayerJS.
        soup = BeautifulSoup(resp.text, "lxml")
        iframe = soup.select_one("iframe")
        if iframe is None or not iframe.get("src"):
            raise ProviderError("parse_failed", "no iframe in player page")
        src = str(iframe["src"])
        if src.startswith("/"):
            src = urljoin(BASE_URL, src)
        # First try the regex extractor on the player page itself.
        extracted = RegexExtractor().extract(resp.text)
        url = extracted.url if extracted and extracted.url else src
        kind = extracted.type if extracted else ("m3u8" if src.endswith(".m3u8") else "hls")
        return StreamResponse(
            url=url,
            type=kind,
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/0.1"},
        )

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
        default_type = "series" if section in {"serials", "animeukr"} else "movie"
        return _parse_listing(resp.text, self.id, default_type)
