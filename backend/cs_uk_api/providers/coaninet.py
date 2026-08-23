"""Coaninet provider (https://coani.net) — Ukrainian-dubbed catalog.
Issue #17, Group 1.

Like Unimay, Coaninet's upstream plugin talks to a JSON API and never
parses HTML for content data. The public content page is a Nuxt SSR
shell that loads the same API on the client; we hit the API directly.

The API moved (2026-08) from ``https://coani.net/api/v1/...`` to
``https://api.coani.net/api/...`` and switched from numeric season ids
to SEO slugs:

  GET /film/catalog?search=<q>       search (Collection of catalog-list)
  GET /film/catalog?page=N           catalog browse (same endpoint for
                                     films and series — the upstream no
                                     longer distinguishes them)
  GET /film/season?slug=<slug>       season detail
  GET /film/season/<id>/series       episode list (one entry per
                                     (episode, voice_type))

External ids are the season SEO slugs (e.g. ``medalist-season-1``);
episode ids embed the season slug plus the episode number:
``coaninet:<season-slug>:<episode>``. Stream URLs come from
``series[*].data.video`` — already an HLS master playlist at
``https://s*.coani.net/hls/<hash>/hls/master.m3u8`` — and play in MPV
with no client-side work.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

import httpx

from ..models import (
    ContentResponse,
    Episode,
    MediaForm,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from ..http_client import provider_safe_get
from ..wire_identity import split_wire_id
from .base import BaseProvider, ProviderError

API_URL = "https://api.coani.net/api"
SITE_URL = "https://coani.net"

# Mirror the upstream Kotlin's `mainPageOf(...)` call: two sections,
# one for films and one for serials. The new catalog endpoint serves
# both from the same route, so the section only labels the row.
COANINET_SECTIONS: tuple[Section, ...] = (
    Section(id="films", title="Фільми", form="movie"),
    Section(id="series", title="Серіали", form="series"),
)

# Type-string value the API uses for serial items.
_TYPE_SERIAL = "serial"

# Per-episode dub/sub labels (the API uses uppercase English codes).
_VOICE_TYPE_LABELS: dict[str, str] = {
    "POLYPHONIC": "Багатоголосся",
    "MONOPHONIC": "Одноголосся",
    "SUB": "Субтитри",
}

# Season SEO slug shape (transliterated, lowercase, hyphens). Anything
# else is an untrusted value and must surface as `not_found` before we
# build a URL with it.
_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# Catalog pagination: the API defaults to limit=10; ask for a full row.
_CATALOG_LIMIT = 24


def _section_url(section: str, page: int) -> str:
    """Build the catalog URL for a section and page.

    The new `/film/catalog` endpoint serves both films and serials
    from the same route; the section argument is accepted for the wire
    contract but does not change the query.
    """
    return f"{API_URL}/film/catalog?page={page}&limit={_CATALOG_LIMIT}"


def _search_url(query: str) -> str:
    # `search` is the real filter param; `q` is ignored by the API.
    return (
        f"{API_URL}/film/catalog?search={quote(query)}"
        f"&page=1&limit={_CATALOG_LIMIT}"
    )


def _season_url(slug: str) -> str:
    return f"{API_URL}/film/season?slug={quote(slug)}"


def _series_url(season_id: int) -> str:
    return f"{API_URL}/film/season/{season_id}/series"


def _parse_card(item: object) -> SearchResult | None:
    """Translate one ``{"type":"catalog-list","data":{...}}`` envelope
    into a ``SearchResult``. Returns None when the envelope is
    malformed (missing slug, etc.). The external id is the season SEO
    slug — the only stable address the new API exposes.
    """
    if not isinstance(item, dict):
        return None
    inner = item.get("data")
    if not isinstance(inner, dict):
        return None
    seo_slug = inner.get("seo_slug")
    if not isinstance(seo_slug, str) or not seo_slug:
        # Catalog payloads expose the season SEO slug; without it we
        # can't resolve the content.
        return None
    name = inner.get("name") or inner.get("film_name") or seo_slug
    preview = inner.get("preview")
    poster: str | None = None
    if isinstance(preview, dict):
        poster = preview.get("preview_main") or preview.get("preview")
    if not poster and isinstance(inner.get("background"), dict):
        bg = inner["background"].get("background")
        if isinstance(bg, str):
            poster = bg
    film_seo_slug = inner.get("film_seo_slug") or seo_slug
    year_value = inner.get("year")
    year = year_value if isinstance(year_value, int) else None
    type_field = inner.get("type")
    media_type: MediaForm = "series" if type_field == _TYPE_SERIAL else "movie"
    return SearchResult(
        id=f"coaninet:{seo_slug}",
        provider="coaninet",
        title=str(name),
        year=year,
        poster=poster,
        url=f"{SITE_URL}/catalog/{film_seo_slug}/{seo_slug}",
        form=media_type,
        styles=frozenset(),
    )


async def _get_json(provider: CoaninetProvider, url: str, http: httpx.AsyncClient) -> object:
    try:
        resp = await provider_safe_get(
            http, provider, url, headers={"Referer": f"{SITE_URL}/"}
        )
    except httpx.HTTPError as e:
        raise ProviderError("unreachable", str(e)) from e
    if resp.status_code >= 500:
        raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
    if resp.status_code != 200:
        raise ProviderError("not_found", f"status {resp.status_code}")
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", f"invalid json: {e}") from e


class CoaninetProvider(BaseProvider):
    id = "coaninet"
    name = "Coaninet"
    types = ("movie", "series")
    sections = COANINET_SECTIONS
    #: The coani.net API + site plus the s1/s3 stream hosts its series
    #: payloads point at (mercure.coani.net is SSE — never fetched by
    #: the backend). Include-don't-exclude per ADR-0005.
    allowed_hosts = frozenset(
        {"coani.net", "api.coani.net", "s1.coani.net", "s3.coani.net"}
    )

    async def search(
        self, query: str, http: httpx.AsyncClient
    ) -> list[SearchResult]:
        data = await _get_json(self, _search_url(query), http)
        items: list[object] = []
        if isinstance(data, dict):
            raw = data.get("data")
            if isinstance(raw, list):
                items = raw
        results: list[SearchResult] = []
        for item in items:
            r = _parse_card(item)
            if r is not None:
                results.append(r)
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        if section not in ("films", "series"):
            raise ProviderError("not_found", f"unknown section: {section}")
        data = await _get_json(self, _section_url(section, page), http)
        items: list[object] = []
        if isinstance(data, dict):
            raw = data.get("data")
            if isinstance(raw, list):
                items = raw
        results: list[SearchResult] = []
        for item in items:
            r = _parse_card(item)
            if r is not None:
                results.append(r)
        # has_next is derived from the meta paginator.
        has_next = False
        if isinstance(data, dict):
            meta = data.get("meta")
            if isinstance(meta, dict):
                paginator = meta.get("paginator")
                if isinstance(paginator, dict):
                    pages = paginator.get("pages")
                    if isinstance(pages, int):
                        has_next = page < pages
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        # external_id is the season SEO slug from the catalog card.
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        season_payload = await _get_json(self, _season_url(external_id), http)
        if (
            not isinstance(season_payload, dict)
            or season_payload.get("type") != "season"
        ):
            raise ProviderError("parse_failed", "season not a season envelope")
        inner = season_payload.get("data")
        if not isinstance(inner, dict):
            raise ProviderError("parse_failed", "season data missing")
        name = inner.get("name") or inner.get("film_name") or external_id
        year_value = inner.get("year")
        year = year_value if isinstance(year_value, int) else None
        preview = inner.get("preview")
        poster: str | None = None
        if isinstance(preview, dict):
            poster = preview.get("preview_main") or preview.get("preview")
        if not poster and isinstance(inner.get("background"), dict):
            bg = inner["background"].get("background")
            if isinstance(bg, str):
                poster = bg
        description = str(
            inner.get("description")
            or inner.get("short_description")
            or ""
        )
        type_field = inner.get("type")
        media_type: MediaForm = "series" if type_field == _TYPE_SERIAL else "movie"

        # Pull the episode list (one entry per (number, voice_type))
        # from the id-addressed series endpoint.
        season_id = inner.get("id")
        seasons: list[Season] | None = None
        if isinstance(season_id, int):
            series_payload = await _get_json(self, _series_url(season_id), http)
            if isinstance(series_payload, dict):
                items = series_payload.get("data") or []
                by_number: dict[int, list[dict[str, object]]] = {}
                for entry in items:
                    if not isinstance(entry, dict):
                        continue
                    d = entry.get("data")
                    if not isinstance(d, dict):
                        continue
                    num = d.get("number")
                    if not isinstance(num, int):
                        continue
                    by_number.setdefault(num, []).append(d)
                if by_number:
                    episodes: list[Episode] = []
                    for num in sorted(by_number):
                        translations: list[Translation] = []
                        for d in by_number[num]:
                            voice_type = d.get("voice_type")
                            if not isinstance(voice_type, str) or not voice_type:
                                continue
                            translations.append(
                                Translation(
                                    id=voice_type,
                                    label=_VOICE_TYPE_LABELS.get(
                                        voice_type, voice_type
                                    ),
                                )
                            )
                        episodes.append(
                            Episode(
                                number=num,
                                id=f"coaninet:{external_id}:{num}",
                                title=f"Серія {num}",
                                translations=translations or None,
                            )
                        )
                    seasons = [Season(number=1, episodes=episodes)]

        # Per-episode translations only when at least one episode has
        # multiple voice types; otherwise stay at content level.
        translations_level = "content"
        if seasons and any(
            e.translations and len(e.translations) > 1
            for e in seasons[0].episodes
        ):
            translations_level = "episode"
        translations = [Translation(id="uk", label="Українська")]
        return ContentResponse(
            id=f"coaninet:{external_id}",
            title=str(name),
            year=year,
            description=description,
            poster=poster,
            translations=translations,
            seasons=seasons,
            translations_level=translations_level,  # type: ignore[arg-type]
            form=media_type,
            styles=frozenset(),
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # content_id format: ``<season-slug>:<episode_number>``
        # (e.g. ``medalist-season-1:1``). A bare slug means episode 1.
        # Voice type is optional and matches the ``voice_type`` field in
        # the series payload (POLYPHONIC / SUB).
        slug, ep_tail = split_wire_id(content_id)
        ep_raw = ep_tail.removeprefix(":")
        if not _SLUG_RE.fullmatch(slug):
            raise ProviderError("not_found", f"bad content_id: {content_id!r}")
        try:
            episode_number = int(ep_raw or "1")
        except ValueError as e:
            raise ProviderError(
                "parse_failed", f"bad content_id: {content_id}"
            ) from e
        # The series endpoint is id-addressed; resolve the slug once.
        season_payload = await _get_json(self, _season_url(slug), http)
        if not isinstance(season_payload, dict):
            raise ProviderError("parse_failed", "season not an object")
        inner = season_payload.get("data")
        season_id = inner.get("id") if isinstance(inner, dict) else None
        if not isinstance(season_id, int):
            raise ProviderError("parse_failed", "season id missing")
        data = await _get_json(self, _series_url(season_id), http)
        if not isinstance(data, dict):
            raise ProviderError("parse_failed", "series not an object")
        items = data.get("data") or []
        candidates: list[dict[str, object]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            d = entry.get("data")
            if not isinstance(d, dict):
                continue
            if d.get("number") == episode_number:
                candidates.append(d)
        if not candidates:
            raise ProviderError(
                "not_found", f"no episode {episode_number} for {slug}"
            )
        chosen: dict[str, object] | None = None
        if translation:
            for c in candidates:
                if str(c.get("voice_type") or "") == translation:
                    chosen = c
                    break
        # Default: first candidate (POLYPHONIC comes before SUB in the
        # upstream payload).
        if chosen is None:
            chosen = candidates[0]
        video_url = chosen.get("video")
        if not isinstance(video_url, str) or not video_url:
            raise ProviderError(
                "parse_failed", f"no video url for episode {episode_number}"
            )
        return StreamResponse(
            url=video_url,
            type="m3u8",
            headers={
                "Referer": f"{SITE_URL}/",
                "User-Agent": "cs-uk-api/1.0",
            },
        )


__all__ = ["CoaninetProvider"]
