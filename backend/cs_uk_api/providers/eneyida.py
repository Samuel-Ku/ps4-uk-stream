"""Eneyida Ukrainian-dubbed film and series provider."""
from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..country import extract_country
from ..http_client import safe_get
from ..models import ContentResponse, Episode, MediaType, SearchResult, Season, Section, StreamResponse, Translation
from .base import BaseProvider, ProviderError, model_b_axes

BASE_URL = "https://eneyida.tv"
_ALLOWED_HOSTS: frozenset[str] = frozenset({"eneyida.tv", "hdvbua.pro"})
ENEYIDA_SECTIONS = (Section(id="films", title="Фільми", type="movie"), Section(id="series", title="Серіали", type="series"))
_PATH_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("serials", "series"), "series"),
    (("films",), "movie"),
)
MOVIE_SUFFIX = ":__movie__"
_SLUG_RE = re.compile(r"\d+-[a-z0-9-]+")
# Upstream's deliberate-unavailable embed page: «Контент недоступний»
# (captured live 2026-08-08 — 1441 bytes, the phrase in <title> and <h1>,
# no `file:` payload). This is upstream-removed content, NOT a provider
# bug, so stream() must surface the `gated` verdict (ADR-0002: a
# deliberate-unavailable is client-side semantics, not an upstream
# failure) instead of `parse_failed`.
_CONTENT_UNAVAILABLE = "Контент недоступний"


def _content_unavailable(html: str) -> bool:
    return _CONTENT_UNAVAILABLE in html


def _page_number(href: str) -> int:
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _external_id_from_url(href: str) -> str | None:
    m = re.search(r"/(?:films|serials)/?(\d+-[a-z0-9-]+)\.html", href)
    if not m:
        m = re.search(r"/(\d+-[a-z0-9-]+)\.html", href)
    if not m:
        return None
    section = "series" if "/serials/" in href else "films"
    return f"{section}/{m.group(1)}"


def _type_from_url(href: str) -> MediaType:
    return "series" if "/serials/" in href or "/series/" in href else "movie"


def _section_url(section: str, page: int) -> str:
    if section not in {"films", "series"}:
        raise ProviderError("not_found", f"unknown section: {section}")
    root = f"{BASE_URL}/{section}/"
    return root if page <= 1 else f"{root}page/{page}/"


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    a = card.select_one("a.short_title") or card.select_one("a.short_img")
    if not a or not a.get("href"):
        return None
    ext = _external_id_from_url(str(a["href"]))
    if not ext:
        return None
    title = a.get_text(" ", strip=True)
    img = card.select_one("img")
    poster_src = (img.get("data-src") or img.get("src")) if img else None
    mb_form, mb_styles = model_b_axes(_type_from_url(str(a["href"])))
    return SearchResult(id=f"{provider_id}:{ext}", provider=provider_id, type=_type_from_url(str(a["href"])), title=title, poster=urljoin(BASE_URL, str(poster_src)) if poster_src else None, url=urljoin(BASE_URL, str(a["href"])), form=mb_form, styles=mb_styles)


def _file_url(html: str) -> str | None:
    m = re.search(r"file\s*:\s*(?:\"([^\"]+)\"|'([^']+)')", html)
    url = m.group(1) or m.group(2) if m else None
    return url


class EneyidaProvider(BaseProvider):
    id = "eneyida"
    name = "Eneyida"
    types = ("movie", "series")
    sections = ENEYIDA_SECTIONS

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        try:
            r = await http.post(f"{BASE_URL}/index.php?do=search", data={"do":"search", "subaction":"search", "story":query})
        except httpx.HTTPError as e: raise ProviderError("unreachable", str(e)) from e
        if r.status_code != 200: raise ProviderError("upstream_unreachable", f"status {r.status_code}")
        return [x for c in BeautifulSoup(r.text, "lxml").select("article.short") if (x := _parse_card(c, self.id))]

    async def browse(self, section: str, page: int, http: httpx.AsyncClient) -> tuple[list[SearchResult], bool]:
        url = _section_url(section, page)
        try: r = await http.get(url)
        except httpx.HTTPError as e: raise ProviderError("unreachable", str(e)) from e
        if r.status_code != 200: raise ProviderError("not_found", f"status {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        results = [x for c in soup.select("article.short") if (x := _parse_card(c, self.id))]
        return results, any(_page_number(str(a.get("href"))) > page for a in soup.select(".navigation a[href]"))

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        kind, _, slug = external_id.partition("/")
        if not kind or not slug: raise ProviderError("parse_failed", "invalid external_id")
        if not _SLUG_RE.fullmatch(slug): raise ProviderError("not_found", "bad external_id")
        try: r = await safe_get(http, f"{BASE_URL}/{kind}/{slug}.html", allowed_hosts=set(_ALLOWED_HOSTS))
        except httpx.HTTPError as e: raise ProviderError("unreachable", str(e)) from e
        if r.status_code != 200: raise ProviderError("not_found", f"status {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml"); h1 = soup.select_one("h1")
        if not h1: raise ProviderError("parse_failed", "title missing")
        country: str | None = extract_country(soup)
        img = soup.select_one(".full img") or soup.select_one("img[src*='/uploads/']")
        iframe = soup.select_one("iframe")
        if not iframe: raise ProviderError("parse_failed", "player missing")
        typ: MediaType = "series" if kind == "series" else "movie"
        seasons: list[Season] | None = [Season(number=1, episodes=[Episode(number=1, id=external_id+MOVIE_SUFFIX, title="Фільм")])]
        if typ == "series": seasons = await self._seasons(str(iframe.get("src")), external_id, http)
        mb_form, mb_styles = model_b_axes(typ)
        return ContentResponse(id=f"{self.id}:{external_id}", type=typ, title=h1.get_text(strip=True), poster=urljoin(BASE_URL, str(img.get("src"))) if img else None, translations=[Translation(id="uk", label="Українська")], seasons=seasons, country=country, form=mb_form, styles=mb_styles)

    async def _seasons(self, player: str, ext: str, http: httpx.AsyncClient) -> list[Season] | None:
        try: r = await http.get(player)
        except httpx.HTTPError: return None
        raw = _file_url(r.text) if r.status_code == 200 else None
        try: data = cast(list[dict[str, Any]], json.loads(raw or "[]")); folders = data[0].get("folder", [])
        except (json.JSONDecodeError, IndexError, AttributeError): return None
        return [Season(number=i, episodes=[Episode(number=j, id=f"{ext}:s{i}e{j}", title=str(e.get("title", "")).strip()) for j,e in enumerate(s.get("folder", []),1)]) for i,s in enumerate(folders,1)]

    async def stream(self, content_id: str, translation: str | None, http: httpx.AsyncClient) -> StreamResponse:
        ext, _, suffix = content_id.partition(":"); kind, _, slug = ext.partition("/")
        if not kind or not slug: raise ProviderError("parse_failed", "invalid content_id")
        if not _SLUG_RE.fullmatch(slug): raise ProviderError("not_found", "bad external_id")
        try: r = await safe_get(http, f"{BASE_URL}/{kind}/{slug}.html", allowed_hosts=set(_ALLOWED_HOSTS))
        except httpx.HTTPError as e: raise ProviderError("unreachable", str(e)) from e
        iframe = BeautifulSoup(r.text, "lxml").select_one("iframe")
        if not iframe: raise ProviderError("parse_failed", "player missing")
        try: p = await safe_get(http, str(iframe.get("src")), allowed_hosts=set(_ALLOWED_HOSTS))
        except httpx.HTTPError as e: raise ProviderError("unreachable", str(e)) from e
        if p.status_code == 200 and _content_unavailable(p.text):
            raise ProviderError("gated", "upstream content removed")
        raw = _file_url(p.text) if p.status_code == 200 else None
        if not raw: raise ProviderError("parse_failed", "media missing")
        if suffix and suffix != "__movie__":
            m = re.fullmatch(r"s(\d+)e(\d+)", suffix)
            if not m: raise ProviderError("parse_failed", "bad episode")
            try: raw = json.loads(raw)[0]["folder"][int(m[1])-1]["folder"][int(m[2])-1]["file"]
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e: raise ProviderError("parse_failed", "episode missing") from e
        return StreamResponse(url=str(raw), type="m3u8", headers={"Referer": "https://eneyida.tv/"})

__all__ = ["EneyidaProvider"]
