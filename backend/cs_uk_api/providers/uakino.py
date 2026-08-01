from __future__ import annotations

import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import ContentResponse, SearchResult, StreamResponse
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
    ) -> ContentResponse:  # implemented in next task
        raise NotImplementedError

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:  # implemented later
        raise NotImplementedError
