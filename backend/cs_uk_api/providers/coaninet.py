"""Coaninet provider (https://coani.net) — Ukrainian-dubbed catalog.
Issue #17, Group 1.

Like Unimay, Coaninet's upstream plugin talks to a JSON API at
``https://coani.net/api/v1/...`` and never parses HTML for content
data. The public content page is a Nuxt SSR shell that loads the same
API on the client; we hit the API directly.

Stream URLs come from ``series[*].data.video`` — already an HLS master
playlist at ``https://s*.coani.net/hls/<hash>/hls/master.m3u8`` —
and play in MPV with no client-side work.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

import httpx

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

API_URL = "https://coani.net/api/v1"
SITE_URL = "https://coani.net"

# Mirror the upstream Kotlin's `mainPageOf(...)` call: two sections,
# one for films and one for serials.
COANINET_SECTIONS: tuple[Section, ...] = (
    Section(id="films", title="Фільми", type="movie"),
    Section(id="series", title="Серіали", type="series"),
)

# Type-string value the API uses for serial items.
_TYPE_SERIAL = "serial"

# Per-episode dub/sub labels (the API uses uppercase English codes).
_VOICE_TYPE_LABELS: dict[str, str] = {
    "POLYPHONIC": "Багатоголосся",
    "MONOPHONIC": "Одноголосся",
    "SUB": "Субтитри",
}


def _page_number(url: str) -> int:
    """Return the ``page=N`` query param of *url*, or 1 if absent.

    Mirrors the same-named helper in the HTML-based providers; kept
    here for symmetry with the section URL builder.
    """
    match = re.search(r"[?&]page=(\d+)", url)
    return int(match.group(1)) if match else 1


def _external_id_from_url(url: str) -> str | None:
    """Return the season id embedded in a ``/api/v1/season/<id>...``
    URL, or None if the URL doesn't match that shape."""
    match = re.search(r"/api/v1/season/(\d+)", url)
    return match.group(1) if match else None


def _section_url(section: str, page: int) -> str:
    """Build the catalog URL for a section and page.

    Upstream convention: ``/api/v1/<films|series>?page=N``.
    """
    return f"{API_URL}/{section}?page={page}"


def _search_url(query: str) -> str:
    return f"{API_URL}/search?q={quote(query)}"


def _season_url(season_id: str) -> str:
    return f"{API_URL}/season/{season_id}"


def _series_url(season_id: str) -> str:
    return f"{API_URL}/season/{season_id}/series"


def _parse_card(item: object) -> SearchResult | None:
    """Translate one ``{"type":"catalog-list","data":{...}}`` envelope
    into a ``SearchResult``. Returns None when the envelope is
    malformed (missing id, etc.)."""
    if not isinstance(item, dict):
        return None
    inner = item.get("data")
    if not isinstance(inner, dict):
        return None
    season_id = inner.get("id")
    if not isinstance(season_id, int):
        # Catalog payloads wrap the season id as int; skip anything
        # else (empty / string fallback) — without an id we can't
        # resolve the content.
        return None
    name = inner.get("name") or inner.get("film_name") or str(season_id)
    preview = inner.get("preview")
    poster: str | None = None
    if isinstance(preview, dict):
        poster = preview.get("preview_main") or preview.get("preview")
    if not poster and isinstance(inner.get("background"), dict):
        bg = inner["background"].get("background")
        if isinstance(bg, str):
            poster = bg
    seo_slug = (
        inner.get("seo_slug")
        or inner.get("film_seo_slug")
        or str(season_id)
    )
    year_value = inner.get("year")
    year = year_value if isinstance(year_value, int) else None
    type_field = inner.get("type")
    media_type: str = "series" if type_field == _TYPE_SERIAL else "movie"
    return SearchResult(
        id=f"coaninet:{season_id}",
        provider="coaninet",
        type=media_type,  # type: ignore[arg-type]
        title=str(name),
        year=year,
        poster=poster,
        url=f"{SITE_URL}/catalog/{seo_slug}/{seo_slug}",
    )


async def _get_json(url: str, http: httpx.AsyncClient) -> object:
    try:
        resp = await http.get(url, headers={"Referer": f"{SITE_URL}/"})
    except httpx.HTTPError as e:
        raise ProviderError("unreachable", str(e)) from e
    if resp.status_code != 200:
        raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", f"invalid json: {e}") from e


class CoaninetProvider(BaseProvider):
    id = "coaninet"
    name = "Coaninet"
    types = ("movie", "series")
    sections = COANINET_SECTIONS

    async def search(
        self, query: str, http: httpx.AsyncClient
    ) -> list[SearchResult]:
        data = await _get_json(_search_url(query), http)
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
        data = await _get_json(_section_url(section, page), http)
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
        season_payload = await _get_json(_season_url(external_id), http)
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
        media_type: str = "series" if type_field == _TYPE_SERIAL else "movie"

        # Pull the episode list and group by number (one entry per
        # (number, voice_type)).
        series_payload = await _get_json(_series_url(external_id), http)
        seasons: list[Season] | None = None
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
            type=media_type,  # type: ignore[arg-type]
            title=str(name),
            year=year,
            description=description,
            poster=poster,
            translations=translations,
            seasons=seasons,
            translations_level=translations_level,  # type: ignore[arg-type]
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # content_id format: ``<season_id>:<episode_number>``
        # (e.g. ``173:1``). Voice type is optional and matches the
        # ``voice_type`` field in series.json (POLYPHONIC / SUB).
        season_id, _, ep_raw = content_id.partition(":")
        try:
            episode_number = int(ep_raw or "1")
        except ValueError as e:
            raise ProviderError(
                "parse_failed", f"bad content_id: {content_id}"
            ) from e
        data = await _get_json(_series_url(season_id), http)
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
                "not_found", f"no episode {episode_number} for {season_id}"
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
                "User-Agent": "cs-uk-api/0.1",
            },
        )


__all__ = ["CoaninetProvider"]