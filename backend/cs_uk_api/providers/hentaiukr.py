"""HentaiUkr provider (https://hentaiukr.com) — NSFW Ukrainian-dubbed
hentai video. Issue #17, Group 4 (in scope per spec; no hiding).

The upstream Kotlin source (HentaiUkrProvider.kt, 5.3 KB) drives the
site via a JSON manifest at ``/search/objects.json``. The ``video``
array is used for both ``mainPage`` and ``search`` (search is a
case-insensitive substring filter on ``it.name``). The content page
(``/video/<slug>/``) carries the title, year, plot and poster as DOM
hooks; the episode list lives in ``<content_url>plur.cfg.json``
where each entry is one episode with multiple MP4 sources (1080/720/480).
"""
from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

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

BASE_URL = "https://hentaiukr.com"
OBJECTS_URL = f"{BASE_URL}/search/objects.json"
CFG_SUFFIX = "plur.cfg.json"

# Per the upstream Kotlin `mainPageOf(objectsUrl to "Хентай")` — exactly
# one section. NSFW rows collapse to `anime` in our v2 contract.
HENTAIUKR_SECTIONS: tuple[Section, ...] = (
    Section(id="hentai", title="Хентай", type="anime"),
)

# Source-size preference. The upstream adds all sources to the
# extractor; we pick the highest available quality. Listed highest
# first so `min(...)` returns the best one.
_QUALITY_RANK: tuple[str, ...] = ("1080", "720", "480")


def _parse_video_array(
    data: dict[str, Any], provider_id: str
) -> list[SearchResult]:
    """Map the upstream ``ObjectsModel.video`` array to ``SearchResult``s.

    Every row on HentaiUkr is NSFW anime; we collapse that to
    ``anime`` for our v2 contract. Missing required fields (id / name /
    url) cause the entry to be skipped rather than raising — the
    upstream always populates them, but a malformed entry should not
    500 the whole listing.
    """
    results: list[SearchResult] = []
    for item in data.get("video") or []:
        if not isinstance(item, dict):
            continue
        if not all(item.get(k) for k in ("id", "name", "url")):
            continue
        mb_form, mb_styles = model_b_axes("anime")
        results.append(
            SearchResult(
                id=f"{provider_id}:{item['id']}",
                provider=provider_id,
                type="anime",
                title=str(item["name"]),
                poster=urljoin(BASE_URL, str(item["thumb"])) if item.get("thumb") else None,
                url=urljoin(BASE_URL, str(item["url"])),
                form=mb_form,
                styles=mb_styles,
            )
        )
    return results


async def _get_json(url: str, http: httpx.AsyncClient) -> object:
    try:
        resp = await http.get(url, headers={"Referer": f"{BASE_URL}/"})
    except httpx.HTTPError as e:
        raise ProviderError("unreachable", str(e)) from e
    if resp.status_code != 200:
        raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", f"invalid json: {e}") from e


async def _fetch_objects(http: httpx.AsyncClient) -> dict[str, Any]:
    data = await _get_json(OBJECTS_URL, http)
    if not isinstance(data, dict):
        raise ProviderError("parse_failed", "objects.json is not an object")
    return cast(dict[str, Any], data)


class HentaiUkrProvider(BaseProvider):
    id = "hentaiukr"
    name = "HentaiUkr 18+"
    types = ("anime",)
    sections = HENTAIUKR_SECTIONS

    async def search(
        self, query: str, http: httpx.AsyncClient
    ) -> list[SearchResult]:
        data = await _fetch_objects(http)
        # Upstream: ``it.name.contains(query, true)`` — case-insensitive
        # substring on the Ukrainian title only (NOT eng_name /
        # orig_name). An empty result must return ``[]`` so the typeahead
        # UX in main.py doesn't crash on a no-match query.
        return [r for r in _parse_video_array(data, self.id) if query.lower() in r.title.lower()]

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        if not self.has_section(section):
            raise ProviderError("not_found", f"unknown section: {section}")
        data = await _fetch_objects(http)
        # The objects.json is a flat list with no pagination cursor;
        # we surface the entire ``video`` array and report no next page.
        _ = page  # the upstream mainPage ignores the page parameter
        return _parse_video_array(data, self.id), False

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        # Reconstruct the slug from the upstream ``video`` array —
        # callers only carry the integer id, but we need the URL slug
        # to hit the content page and the cfg.json.
        data = await _fetch_objects(http)
        item = next(
            (v for v in (data.get("video") or []) if str(v.get("id")) == external_id),
            None,
        )
        if item is None:
            raise ProviderError("not_found", f"unknown id: {external_id}")
        content_url = urljoin(BASE_URL, str(item["url"]))
        try:
            resp = await http.get(content_url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        # Title: ``#name-ukr`` is the Ukrainian name (the upstream
        # Kotlin uses this). The ``<title>`` tag has a " | Хентай
        # аніме українською" suffix, so we must NOT use it.
        title_el = soup.select_one("#name-ukr")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        title = title_el.get_text(strip=True)
        year: int | None = None
        year_el = soup.select_one("#year")
        if year_el is not None:
            year_m = re.search(r"\b(19|20)\d{2}\b", year_el.get_text(" ", strip=True))
            if year_m:
                year = int(year_m.group(0))
        poster_el = soup.select_one("#img-placeholder img") or soup.select_one("#img")
        poster = (
            urljoin(BASE_URL, str(poster_el["src"]))
            if poster_el and poster_el.get("src")
            else None
        )
        # Description: the upstream Kotlin reads ``document.select("#about").text()``
        # as the series plot. Both captured fixtures happen to be empty
        # (the upstream site renders an empty div for some manga-only
        # entries), but real content pages do carry text here.
        desc_el = soup.select_one("#about")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""
        cfg_data = await _get_json(content_url + CFG_SUFFIX, http)
        episodes: list[Episode] = []
        if isinstance(cfg_data, list):
            # 1-based index matches the upstream `episode = index + 1`.
            for idx, entry in enumerate(cfg_data, start=1):
                if isinstance(entry, dict):
                    episodes.append(
                        Episode(
                            number=idx,
                            id=f"{self.id}:{external_id}:{idx}",
                            title=f"Серія {idx}",
                        )
                    )
        # HentaiUkr is single-dub (Ukrainian). One Translation row so
        # /api/content passes ``min_length=1`` on ``translations``.
        mb_form, mb_styles = model_b_axes("anime")
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            type="anime",
            title=title,
            year=year,
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            form=mb_form,
            styles=mb_styles,
            seasons=[Season(number=1, episodes=episodes)] if episodes else None,
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # ``content_id`` arrives as ``<external_id>:<episode_number>``,
        # or as a bare ``<external_id>`` straight from a search result —
        # in that case the upstream default (episode 1) applies.
        # The upstream Kotlin uses ``<url>, <index>`` with the URL
        # carried alongside the index; we replace the URL with the
        # integer id because the URL slug is not part of the
        # `SearchResult.id` we exposed to clients. 1-based indices.
        _ = translation  # single-dub site; translation is ignored
        external_id, _, ep_raw = content_id.partition(":")
        if not external_id:
            raise ProviderError("parse_failed", f"bad content_id: {content_id}")
        if ep_raw:
            try:
                episode_number = int(ep_raw)
            except ValueError as e:
                raise ProviderError("parse_failed", f"bad content_id: {content_id}") from e
        else:
            episode_number = 1
        data = await _fetch_objects(http)
        item = next(
            (v for v in (data.get("video") or []) if str(v.get("id")) == external_id),
            None,
        )
        if item is None:
            raise ProviderError("not_found", f"unknown id: {external_id}")
        content_url = urljoin(BASE_URL, str(item["url"]))
        cfg_data = await _get_json(content_url + CFG_SUFFIX, http)
        if not isinstance(cfg_data, list):
            raise ProviderError("parse_failed", "cfg.json not a list")
        if not (1 <= episode_number <= len(cfg_data)):
            # REGRESSION (KinoTron): must raise not_found — do not
            # silently fall back to the first available episode.
            raise ProviderError(
                "not_found",
                f"episode {episode_number} out of range (1..{len(cfg_data)})",
            )
        entry = cfg_data[episode_number - 1]
        if not isinstance(entry, dict):
            raise ProviderError("parse_failed", "cfg entry not an object")
        sources = entry.get("sources") or []
        if not isinstance(sources, list) or not sources:
            raise ProviderError("parse_failed", "no sources for episode")
        # Highest-quality source wins. The upstream adds all sources
        # to the extractor; we only return one URL.
        valid = [s for s in sources if isinstance(s, dict) and s.get("src")]
        best = min(
            valid,
            key=lambda s: _QUALITY_RANK.index(str(s.get("size")))
            if str(s.get("size")) in _QUALITY_RANK
            else len(_QUALITY_RANK),
        )
        return StreamResponse(
            url=urljoin(content_url, str(best["src"])),
            type="mp4",
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/0.1"},
        )


__all__ = ["HentaiUkrProvider"]