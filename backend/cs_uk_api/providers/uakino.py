from __future__ import annotations

import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import ContentResponse, Episode, SearchResult, Season, StreamResponse, Translation
from .base import BaseProvider, ProviderError

BASE_URL = "https://uakino.club"


def _external_id_from_url(href: str) -> str:
    m = re.search(r"/(film|serial)/([\w-]+)", href)
    if not m:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return f"{m.group(1)}-{m.group(2)}"


def _is_series(href: str) -> bool:
    return "/serial/" in href


class UakinoProvider(BaseProvider):
    id = "uakino"
    name = "Uakino"
    types = ("movie", "series")

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
    ) -> StreamResponse:  # implemented later
        raise NotImplementedError
