"""Unimay provider (https://unimay.media) — Ukrainian-dubbed anime and
anime films. Issue #17, Group 1.

Unlike the HTML-scraper providers (Uakino, UFDub), Unimay's upstream
plugin talks to a JSON API at ``https://api.unimay.media/v1/...`` and
never parses HTML. The search, content, and stream methods are all
thin ``httpx`` wrappers around the API. Stream URLs come from
``episode.hls.master`` (an HLS master playlist) and play in MPV with
no client-side work.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import httpx

from ..http_client import provider_safe_get
from ..models import (
    ContentResponse,
    Episode,
    MediaForm,
    MediaStyle,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from ..wire_identity import split_wire_id
from .base import BaseProvider, ProviderError

API_URL = "https://api.unimay.media"
MAIN_URL = "https://www.unimay.media"
IMG_URL = "https://img.unimay.media"

# Per the upstream Kotlin's `mainPageOf(...)` call: two sections,
# both typed as `anime` because every row on Unimay is an anime.
UNIMAY_SECTIONS: tuple[Section, ...] = (
    Section(id="updates", title="Останні релізи", styles=frozenset({"anime"})),
    Section(id="projects", title="Наші проєкти", styles=frozenset({"anime"})),
)

# Type-string values in the API's `release.type` field. Anything else
# falls back to `anime` (Unimay is anime-only).
_TYPE_FILM = "Фільм"
_TYPE_SERIES = "Телесеріал"


def _type_from_api(type_field: str | None) -> str:
    """Map the upstream `release.type` field to our MediaType.

    The Kotlin source maps both `Фільм` (film) and `Телесеріал` (TV
    series) to ``TvType.Anime`` / ``TvType.AnimeMovie``, but the v2
    contract separates ``movie`` from ``anime``.
    """
    if type_field == _TYPE_FILM:
        return "movie"
    if type_field == _TYPE_SERIES:
        return "anime"
    return "anime"


def _unimay_axes(api_type: str) -> tuple[MediaForm, frozenset[MediaStyle]]:
    """Model B axes for a unimay item. Unimay is an anime-only site:
    ``movie`` means an anime film, ``anime`` an anime series — the
    anime style tag applies to everything."""
    form: MediaForm = "movie" if api_type == "movie" else "series"
    return form, frozenset({"anime"})


def _poster_url(uuid: str | None, *, width: int = 640) -> str | None:
    """Build the img.unimay.media poster URL with the CDN's preferred
    width + format. Used for both search/mainPage (640) and content
    page banner (2560)."""
    if not uuid:
        return None
    return f"{IMG_URL}/{uuid}?width={width}&format=webp"


def _project_url(code: str) -> str:
    return f"{MAIN_URL}/projects/{code}"


def _search_url(query: str) -> str:
    # Live-gate regression (2026-08-01): the API no longer answers
    # `title=` (always 0 hits); `query=` is the working param.
    return f"{API_URL}/v1/release/search?query={quote(query)}"


def _projects_url(page: int) -> str:
    # Upstream: `$apiUrl/v1/release/search?page_size=10&page=` then
    # `request.data + page`. 1-based page index, so `page=1` is the
    # first page.
    return f"{API_URL}/v1/release/search?page_size=10&page={page}"


def _release_url(code: str) -> str:
    return f"{API_URL}/v1/release?code={quote(code)}"


async def _get_json(provider: UnimayProvider, url: str, http: httpx.AsyncClient) -> object:
    try:
        resp = await provider_safe_get(
            http, provider, url, headers={"Referer": f"{MAIN_URL}/"}
        )
    except httpx.HTTPError as e:
        raise ProviderError("unreachable", str(e)) from e
    if resp.status_code != 200:
        raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", f"invalid json: {e}") from e


class UnimayProvider(BaseProvider):
    id = "unimay"
    name = "Unimay"
    types = ("movie", "anime")
    sections = UNIMAY_SECTIONS
    #: The unimay API/site plus its image + HLS CDN hosts — include-
    #: rather-than-exclude so any future fetch of a returned CDN URL
    #: still fails safe instead of open (ADR-0005).
    allowed_hosts = frozenset(
        {"api.unimay.media", "www.unimay.media", "img.unimay.media", "cdn.unimay.media"}
    )
    # v3 (issue #70): "Останні релізи" contributes to «Новинки».
    newest_section = "updates"

    async def search(
        self, query: str, http: httpx.AsyncClient
    ) -> list[SearchResult]:
        data = await _get_json(self, _search_url(query), http)
        results: list[SearchResult] = []
        # The Kotlin casts to `SearchModel::class.java` and walks
        # `.content`. Items lacking a `code` (rare) are skipped.
        for item in (data.get("content") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not code:
                continue
            names = item.get("names") or {}
            images = item.get("images") or {}
            mb_form, mb_styles = _unimay_axes(_type_from_api(item.get("type")))
            results.append(
                SearchResult(
                    id=f"{self.id}:{code}",
                    provider=self.id,
                    title=str(names.get("ukr") or names.get("eng") or code),
                    year=item.get("year"),
                    poster=_poster_url(images.get("poster")),
                    url=_project_url(code),
                    form=mb_form,
                    styles=mb_styles,
                )
            )
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        if section == "updates":
            return await self._browse_updates(page, http)
        if section == "projects":
            return await self._browse_projects(page, http)
        raise ProviderError("not_found", f"unknown section: {section}")

    async def _browse_updates(
        self, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        # Upstream: page != 1 for the updates section returns empty.
        if page != 1:
            return [], False
        data = await _get_json(self, 
            f"{API_URL}/v1/list/series/updates?size=15", http
        )
        results: list[SearchResult] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            release = item.get("release") or {}
            code = release.get("code")
            if not code:
                continue
            mb_form, mb_styles = _unimay_axes(_type_from_api(release.get("type")))
            results.append(
                SearchResult(
                    id=f"{self.id}:{code}",
                    provider=self.id,
                    title=str(release.get("name") or code),
                    poster=_poster_url(release.get("posterUuid")),
                    url=_project_url(code),
                    form=mb_form,
                    styles=mb_styles,
                )
            )
        # Updates section is never paginated.
        return results, False

    async def _browse_projects(
        self, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        data = await _get_json(self, _projects_url(page), http)
        results: list[SearchResult] = []
        for item in (data.get("content") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not code:
                continue
            names = item.get("names") or {}
            images = item.get("images") or {}
            mb_form, mb_styles = _unimay_axes(_type_from_api(item.get("type")))
            results.append(
                SearchResult(
                    id=f"{self.id}:{code}",
                    provider=self.id,
                    title=str(names.get("ukr") or names.get("eng") or code),
                    year=item.get("year"),
                    poster=_poster_url(images.get("poster")),
                    url=_project_url(code),
                    form=mb_form,
                    styles=mb_styles,
                )
            )
        # The API's `last` field tells us if more pages exist.
        has_next = bool(isinstance(data, dict) and not data.get("last", True))
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        data = await _get_json(self, _release_url(external_id), http)
        if not isinstance(data, dict):
            raise ProviderError("parse_failed", "release not an object")
        names = data.get("names") or {}
        images = data.get("images") or {}
        title = str(names.get("ukr") or names.get("eng") or external_id)
        media_type = _type_from_api(data.get("type"))
        seasons: list[Season] | None = None
        playlist = data.get("playlist") or []
        if playlist and media_type in ("anime", "series"):
            episodes = [
                Episode(
                    number=int(p.get("number") or idx + 1),
                    id=f"{self.id}:{external_id}:{p.get('number') or idx + 1}",
                    title=str(p.get("title") or f"Серія {p.get('number') or idx + 1}"),
                )
                for idx, p in enumerate(playlist)
                if isinstance(p, dict) and not p.get("premium", False)
            ]
            if episodes:
                seasons = [Season(number=1, episodes=episodes)]
        mb_form, mb_styles = _unimay_axes(media_type)
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            title=title,
            year=data.get("year"),
            description=str(data.get("description") or ""),
            poster=_poster_url(images.get("banner"), width=2560),
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            form=mb_form,
            styles=mb_styles,
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # content_id format: ``<code>:<episode_number>`` (e.g.
        # ``dandadan:1``). The upstream splits on ``", "`` (comma +
        # space) — we use ``:`` since the code can contain hyphens and
        # ``:`` is guaranteed not to appear in any release code.
        code, ep_tail = split_wire_id(content_id)
        ep_raw = ep_tail.removeprefix(":")
        try:
            episode_number = int(ep_raw or "1")
        except ValueError as e:
            raise ProviderError("parse_failed", f"bad content_id: {content_id}") from e
        data = await _get_json(self, _release_url(code), http)
        if not isinstance(data, dict):
            raise ProviderError("parse_failed", "release not an object")
        playlist = data.get("playlist") or []
        episode = next(
            (
                p
                for p in playlist
                if isinstance(p, dict) and p.get("number") == episode_number
            ),
            None,
        )
        if episode is None:
            # Fall back to the first non-premium entry. Matches the
            # upstream behaviour for movies (single playlist entry,
            # number=1) and tolerates typos in the requested number.
            episode = next(
                (
                    p
                    for p in playlist
                    if isinstance(p, dict) and not p.get("premium", False)
                ),
                None,
            )
        if episode is None:
            raise ProviderError("not_found", f"no episode for {content_id}")
        hls = episode.get("hls") or {}
        url = hls.get("master")
        if not url:
            raise ProviderError("parse_failed", "episode has no hls.master")
        return StreamResponse(
            url=str(url),
            type="m3u8",
            headers=self.stream_headers(f"{MAIN_URL}/"),
        )


__all__ = ["UnimayProvider"]