"""AnimeON provider (https://animeon.club) — JSON-API-backed Ukrainian
anime catalogue. Issue #17, Group 3+.

AnimeON is a thin SPA around a private REST API; there are no HTML
listing pages. All endpoints return JSON and are pure HTTP GETs, so
the provider is portable under the v2 rule "HTML scraping + iframe
/regex only".

Endpoints used:

  GET /api/anime?search=<q>          SafeSearchApiResponse
  GET /api/anime?pageSize=24&pageIndex=N   SafeNewAnimeModel
  GET /api/anime/seasons             List<LocalResult>
  GET /api/stats/anime/<date>?withView=false  List<LocalResult>
  GET /api/anime/<id-or-slug>        RedirectResponse or SafeAnimeInfoModel
  GET /api/anime/<slug>/episodes-info    List<EpisodeInfo>
  GET /api/player/<id>/translations  SafeTranslationsResponse
  GET /api/player/<id>/episodes?take=N&playerId=P&translationId=T&skip=K
                                     SafePlayerEpisodes

External id shape: a bare integer (``913``) — the search JSON does not
expose the slug, and resolving it per result would inflate search cost.
``content()`` resolves the redirect to a slug once and uses that for
the rest of the calls.

The Moon player hosts a Playerjs whose ``atob("...")`` blob is XOR
encrypted with a 1-byte state + 32-byte sliding key — the upstream
``moonOuterDecode``. The decoded JS reveals ``var k = "<xor_key>"``
and ``_0xd("<b64>")`` calls that decode the actual stream URL with a
simple XOR cycle against ``k``. Both ciphers are reimplemented in
``_moon_outer_decode`` / ``_moon_decrypt`` (stdlib only, no JS engine).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from datetime import datetime
from typing import Any, cast
from urllib.parse import quote_plus

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
from ..wire_identity import MOVIE_SUFFIX
from .base import BaseProvider, MediaTypeStr, ProviderError, ProviderErrorCode, model_b_axes

BASE_URL = "https://animeon.club"
MOON_BASE = "https://moonanime.art"
ASHDI_REFERER = "https://ashdi.vip/"
MOON_REFERER = "https://moonanime.art/"

logger = logging.getLogger(__name__)

# v2 sections mirror the upstream `mainPageOf(...)` slots:
#  * "seasons"   -> /api/anime/seasons                 (List<LocalResult>)
#  * "popular"   -> /api/stats/anime/<date>?withView=false
#  * "page"      -> /api/anime?pageSize=24&pageIndex=N (paginated)
ANIMEON_SECTIONS: tuple[Section, ...] = (
    Section(id="seasons", title="Сезон", styles=frozenset({"anime"})),
    Section(id="popular", title="Популярні", styles=frozenset({"anime"})),
    Section(id="page", title="Нове аніме", styles=frozenset({"anime"})),
)

# Bare integer ids only (matches the v1 Kotlin's
# substringAfterLast("/").substringBefore("-").toIntOrNull()).
_EXTERNAL_ID_RE = re.compile(r"\d{1,8}")

#: Bounded concurrency for the episode-map walk (issue #187). A long
#: archive like One Piece spans 11 (translation, player) pairs and
#: ~55 upstream pages; the old fully-sequential walk took 10s+ and
#: 502'd whenever the upstream throttled. Pages are fetched in
#: bounded-concurrency windows through ONE shared semaphore (same
#: pattern as simpsonsuatv's season fetch, issue #119).
_EPISODE_FETCH_CONCURRENCY = 6

# Slug shape returned by the bare-id redirect at /api/anime/<id>:
# upstream joins the numeric id with the URL-safe title via ``-``.
# Anything that doesn't match this pattern is an untrusted value
# (path traversal, JSON injection in the next URL we build) and must
# surface as ``not_found`` before we issue the follow-up GET.
_SLUG_RE = re.compile(r"\d{1,8}-[a-z0-9][a-z0-9-]{0,80}")

# Playerjs iframe's obfuscation payload:
#   atob("...==")                            (outer, moonOuterDecode)
#   var k = "..."                            (inner XOR key)
#   _0xd("...")                              (inner XOR, moonDecrypt)
_ATOB_RE = re.compile(r"""atob\(\s*["']([A-Za-z0-9+/=]+)["']\s*\)""")
_KEY_RE = re.compile(r"""var\s+k\s*=\s*["']([^"']+)["']""")
_INNER_RE = re.compile(r"""_0xd\s*\(\s*["']([^"']+)["']\s*\)""")

# Playerjs iframe's `file:'<url>'` m3u8 (used as the Ashdi fallback
# when the JSON `fileUrl` is missing).
_ASHDI_FILE_RE = re.compile(r"""file\s*:\s*['"]([^'"]+\.m3u8)['"]""")

# Common headers the upstream Kotlin attaches to every call to
# animeon.club or to the player CDNs (kept in sync with the upstream
# `userAgent` constant).
_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{BASE_URL}/",
}


def _moon_outer_decode(blob: str) -> bytes:
    """Reimplementation of the upstream Kotlin ``moonOuterDecode``.

    The Playerjs ``atob("...")`` payload is base64 of::

        [state0:u8][key:32 bytes][data:N bytes]

    Each data byte is XORed with ``key[i % 32] ^ state`` and the
    state is updated to ``(data[i] + key[i % 32]) & 0xFF``. The result
    is the JavaScript body that contains ``var k`` and ``_0xd`` calls.
    """
    raw = base64.b64decode(blob)
    if len(raw) < 33:
        return b""
    state = raw[0]
    key = raw[1:33]
    data = raw[33:]
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        k = key[i % 32]
        out[i] = (byte ^ k ^ state) & 0xFF
        state = (byte + k) & 0xFF
    return bytes(out)


def _moon_decrypt(blob: str, xor_key: str) -> str:
    """Reimplementation of the upstream Kotlin ``moonDecrypt``.

    The inner cipher is base64-decode + cyclic XOR with the key string.
    The decoded text usually is a URL or a JSON snippet; failures are
    swallowed (returns ``""``) because the upstream Kotlin does the
    same.
    """
    try:
        raw = base64.b64decode(blob)
    except (ValueError, binascii.Error):
        return ""
    out = bytearray(len(raw))
    keys = [ord(c) for c in xor_key]
    for i, byte in enumerate(raw):
        out[i] = (byte ^ keys[i % len(keys)]) & 0xFF
    return out.decode("utf-8", errors="ignore")


def _today_string() -> str:
    """Upstream passes ``EEE MMM dd yyyy`` to ``/api/stats/anime/`` —
    replicate the locale-stable English format."""
    return datetime.now().astimezone().strftime("%a %b %d %Y").replace(" 0", "  ")


def _poster_url(preview: str | None) -> str | None:
    """Build the canonical poster URL from the API's ``image.preview``
    filename — the upstream posterApi template is
    ``$mainUrl/api/uploads/images/<preview>``."""
    if not preview:
        return None
    return f"{BASE_URL}/api/uploads/images/{preview}"


def _form_from_type(raw_type: Any) -> MediaForm:
    """Map an upstream listing item's ``type`` onto the Model B form axis.

    Issue #140 — listing cards must agree with ``content()`` on the form
    axis for the same id: ``type == "movie"`` is an anime film
    (``form="movie"``); every other value (``tv`` / ``ova`` / ``ona`` /
    ``special``) and a missing field (older captures such as the
    /api/anime/seasons shape) are episodic (``form="series"``).
    Styles stay ``{anime}`` — animeon is an anime-only catalogue — and
    are supplied by ``model_b_axes("anime")`` callers; ``content()``
    derives its form from the same ``type == "movie"`` check, so a
    card and its detail never disagree."""
    return "movie" if str(raw_type or "").strip().lower() == "movie" else "series"


def _classify_translations(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify a raw ``/api/player/<id>/translations`` payload.

    A ``translations`` key that is present but empty (``{"translations":
    []}``) is deliberate upstream withholding — live capture 2026-08-08:
    animeon 8096 "Коджін Сенші Оредам" answers exactly this — so it
    raises ``gated`` (ADR-0002: client-side 404, never a health signal).
    A missing or malformed key is an upstream shape change and returns
    ``[]`` for the caller to surface ``parse_failed`` instead."""
    raw = doc.get("translations")
    if isinstance(raw, list):
        if not raw:
            raise ProviderError("gated", "no translations")
        return raw
    return []


def _is_ashdi(source: dict[str, Any]) -> bool:
    name = str(source.get("player_name", ""))
    return "ashdi" in name.lower()


def _is_moon(source: dict[str, Any]) -> bool:
    name = str(source.get("player_name", ""))
    return "moon" in name.lower()


class AnimeONProvider(BaseProvider):
    id = "animeon"
    name = "AnimeON"
    types: tuple[MediaTypeStr, ...] = ("anime",)
    sections = ANIMEON_SECTIONS
    # v3 (issue #70): the ``page`` section is animeon's "Нове аніме"
    # listing — contributes to the «Новинки» row.
    newest_section = "page"
    #: ``content()`` gates titles whose upstream translations are
    #: withheld (``{"translations": []}`` → ``gated``, ADR-0002). The
    #: catalog sweep must run for animeon so those dead cards are
    #: dropped from home/search instead of failing only at play time
    #: (#160, same pattern as eneyida #158).
    can_gate = True

    async def _get_json(
        self,
        url: str,
        http: httpx.AsyncClient,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET ``url`` and parse the JSON body. Raises ``ProviderError``
        with the right code: ``unreachable`` on connection errors,
        ``upstream_unreachable`` on 5xx, and ``not_found`` on 4xx."""
        try:
            response = await http.get(url, headers=headers or _DEFAULT_HEADERS)
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code >= 500:
            raise ProviderError(
                "upstream_unreachable", f"status {response.status_code}"
            )
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        try:
            return response.json()
        except json.JSONDecodeError as error:
            raise ProviderError("parse_failed", str(error)) from error

    async def _get_text(
        self,
        url: str,
        http: httpx.AsyncClient,
        *,
        headers: dict[str, str],
    ) -> str:
        """GET ``url`` and return the body as text. Same error codes as
        ``_get_json`` except we don't try to JSON-decode the body."""
        try:
            response = await http.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code >= 500:
            raise ProviderError(
                "upstream_unreachable", f"status {response.status_code}"
            )
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        return response.text

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # quote_plus escapes every URL-unsafe char (including &, ?, #,
        # space) so the upstream never sees an injected query parameter.
        encoded = quote_plus(query, safe="")
        data = await self._get_json(f"{BASE_URL}/api/anime?search={encoded}", http)
        results = (data or {}).get("results", []) if isinstance(data, dict) else []
        out: list[SearchResult] = []
        for item in results:
            if "id" not in item:
                continue
            title = str(item.get("titleUa") or "").strip()
            if not title:
                continue
            # Issue #140: derive the form axis per-item from the upstream
            # `type` field so search cards agree with content() for the
            # same id. Styles stay {anime} (animeon is anime-only).
            mb_form, mb_styles = model_b_axes(
                "anime", form=_form_from_type(item.get("type"))
            )
            out.append(SearchResult(
                id=f"{self.id}:{item['id']}",
                provider=self.id,
                title=title,
                poster=_poster_url((item.get("image") or {}).get("preview")),
                form=mb_form,
                styles=mb_styles,
                url=f"{BASE_URL}/anime/{item['id']}",
            ))
        return out

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        if section == "seasons":
            data = await self._get_json(f"{BASE_URL}/api/anime/seasons", http)
            results = data if isinstance(data, list) else []
            return self._build_local_results(results), False
        if section == "popular":
            data = await self._get_json(
                f"{BASE_URL}/api/stats/anime/{_today_string()}?withView=false",
                http,
            )
            results = data if isinstance(data, list) else []
            return self._build_local_results(results), False
        if section == "page":
            data = await self._get_json(
                f"{BASE_URL}/api/anime?pageSize=24&pageIndex={page}", http
            )
            items = (data or {}).get("results", []) if isinstance(data, dict) else []
            total = (
                int((data or {}).get("totalCount", 0)) if isinstance(data, dict) else 0
            )
            has_next = (page * 24) < total and len(items) >= 24
            return self._build_safe_results(items), has_next
        raise ProviderError("not_found", f"unknown section: {section}")

    @staticmethod
    def _build_local_results(items: list[dict[str, Any]]) -> list[SearchResult]:
        out: list[SearchResult] = []
        for item in items:
            if "id" not in item:
                continue
            image = item.get("image") or {}
            # Issue #140: default to "series" when `type` is absent (older
            # LocalResult captures like seasons.json carry no `type` key);
            # `movie` maps to form="movie", everything else stays "series".
            mb_form, mb_styles = model_b_axes("anime", form=_form_from_type(item.get("type")))
            out.append(
                SearchResult(
                    id=f"{ANIMEON_ID}:{item['id']}",
                    provider="animeon",
                    title=str(item.get("titleUa", "")).strip(),
                    poster=_poster_url(image.get("preview")),
                    form=mb_form,
                    styles=mb_styles,
                    url=f"{BASE_URL}/anime/{item['id']}",
                )
            )
        return out

    @staticmethod
    def _build_safe_results(items: list[dict[str, Any]]) -> list[SearchResult]:
        return AnimeONProvider._build_local_results(items)

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        anime_id = int(external_id)

        info, year_int = await self._load_content_info(anime_id, external_id, http)
        title = str(info.get("titleUa") or "").strip()
        if not title:
            raise ProviderError("parse_failed", "title missing")
        image = info.get("image") or {}
        poster = _poster_url(image.get("preview"))
        description = str(info.get("description") or "")
        # Ticket #232: the upstream genres[] carries {nameEn, nameUa,
        # slug} — surface the Ukrainian names so the detail page's
        # genre row renders (was never parsed).
        genres = [
            str(g.get("nameUa") or "").strip()
            for g in (info.get("genres") or [])
            if isinstance(g, dict) and str(g.get("nameUa") or "").strip()
        ]

        if str(info.get("type") or "").strip().lower() == "movie":
            return await self._movie_content(
                anime_id, external_id, title, year_int, description, poster, genres, http
            )

        episodes_by_num = await self._collect_episode_map(anime_id, http)
        if not episodes_by_num:
            raise ProviderError("parse_failed", "no episodes resolved")

        # Ticket #223: the upstream ``/api/anime/<slug>/episodes-info``
        # endpoint carries per-episode real titles (``titleUa``) and air
        # dates (``aired``) — the player-episode walk only yields "Серія
        # N". Best-effort: a failure or 404 degrades to the generic
        # titles (never gates the card).
        slug = str(info.get("slug") or external_id)
        episode_info = await self._episode_info(slug, http)

        all_translations = sorted(
            {
                str(entry["translation_name"])
                for entries in episodes_by_num.values()
                for entry in entries
            }
        )
        season = self._build_season(
            anime_id, episodes_by_num, all_translations, self.id,
            episode_info=episode_info,
        )
        mb_form, mb_styles = model_b_axes("anime")
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            title=title,
            year=year_int,
            description=description,
            poster=poster,
            genres=genres,
            translations=[
                Translation(id=name, label=name) for name in all_translations
            ],
            seasons=[season],
            translations_level="episode",
            form=mb_form,
            styles=mb_styles,
        )

    @staticmethod
    def _build_season(
        anime_id: int,
        episodes_by_num: dict[int, list[dict[str, Any]]],
        all_translations: list[str],
        provider_id: str,
        *,
        episode_info: dict[int, dict[str, Any]] | None = None,
    ) -> Season:
        info = episode_info or {}
        return Season(
            number=1,
            episodes=[
                AnimeONProvider._build_episode(
                    anime_id=anime_id,
                    episode_num=ep_num,
                    entries=sorted(
                        entries,
                        key=lambda e: (
                            str(e["translation_name"]),
                            str(e["player_name"]),
                        ),
                    ),
                    translations=all_translations,
                    provider_id=provider_id,
                    ep_info=info.get(ep_num),
                )
                for ep_num, entries in sorted(episodes_by_num.items())
            ],
        )

    async def _movie_content(
        self,
        anime_id: int,
        external_id: str,
        title: str,
        year: int | None,
        description: str,
        poster: str | None,
        genres: list[str],
        http: httpx.AsyncClient,
    ) -> ContentResponse:
        """Movies have no episode list upstream (``/api/player/<id>/
        episodes`` returns an empty array); the card must still resolve
        to a viewable detail — poster, studio list, Movie form — so the
        facade degrades to a season-less response instead of a 404."""
        payload = await self._ask_translations(anime_id, http)
        # Same gating as stream(): a present-but-empty `translations`
        # list is deliberate upstream withholding (`gated`, issue #160)
        # — the card must not surface with a fake «Оригінал» track that
        # can never play. A missing key (shape change) keeps the detail
        # viewable with the default track, exactly like stream().
        translations = _classify_translations(payload or {})
        names: list[str] = []
        for trans in translations:
            name = str((trans.get("translation") or {}).get("name") or "").strip()
            if name and name not in names:
                names.append(name)
        if not names:
            names = ["Оригінал"]
        # AnimeON movies are anime films — form=movie, styles={anime}
        # (the default model_b_axes for "movie" would drop the style).
        mb_form, mb_styles = model_b_axes("anime", form="movie")
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            title=title,
            year=year,
            description=description,
            poster=poster,
            genres=genres,
            translations=[Translation(id=name, label=name) for name in names],
            form=mb_form,
            styles=mb_styles,
            seasons=None,
            translations_level="content",
        )

    async def _load_content_info(
        self, anime_id: int, external_id: str, http: httpx.AsyncClient
    ) -> tuple[dict[str, Any], int | None]:
        """Resolve the bare-id redirect to a slug, fetch the canonical
        content JSON, and parse the year integer. The upstream
        Kotlin's `resolveAnimeApiUrl` does the same redirect dance."""
        first = await self._get_json(f"{BASE_URL}/api/anime/{anime_id}", http)
        if isinstance(first, dict) and first.get("moved") is True:
            slug = str(first.get("slug") or first.get("redirectTo") or "")
            if not _SLUG_RE.fullmatch(slug):
                raise ProviderError("not_found", "bad redirect slug")
        else:
            slug = external_id

        info = await self._get_json(f"{BASE_URL}/api/anime/{slug}", http)
        if not isinstance(info, dict):
            raise ProviderError("parse_failed", "content body not object")

        raw_year = info.get("releaseDate")
        year_int: int | None = None
        if isinstance(raw_year, str) and raw_year:
            # The upstream sends either a full ISO date ("2002-10-03")
            # or a bare year ("2002") — both must surface as the
            # ProductionYear (ticket #232).
            m = re.search(r"(19\d{2}|20\d{2})", raw_year)
            if m:
                year_int = int(m.group(1))
        return info, year_int

    async def _episode_info(
        self, slug: str, http: httpx.AsyncClient
    ) -> dict[int, dict[str, Any]]:
        """Best-effort ``/api/anime/<slug>/episodes-info`` (ticket #223).

        Returns ``{episode_number: {title, titleUa, aired}}`` from the
        upstream's per-episode metadata endpoint. Any failure — the
        endpoint missing for a title, an upstream error, a malformed
        body — degrades to ``{}`` so content() keeps the generic
        "Серія N" titles; per-episode enrichment must never gate a
        card that is otherwise playable.
        """
        try:
            raw = await self._get_json(
                f"{BASE_URL}/api/anime/{slug}/episodes-info", http
            )
        except Exception:  # noqa: BLE001
            # Best-effort by contract: a missing endpoint (some titles
            # 404 it), an upstream error, OR a test double that rejects
            # the URL (respx AllMockedAssertionError is not an httpx
            # error) must never gate a playable card — degrade to the
            # generic "Серія N" titles instead.
            return {}
        if not isinstance(raw, list):
            return {}
        out: dict[int, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                num = int(entry.get("episode") or 0)
            except (TypeError, ValueError):
                continue
            if num < 1:
                continue
            out[num] = {
                "title": str(entry.get("title") or ""),
                "titleUa": str(entry.get("titleUa") or ""),
                "aired": entry.get("aired"),
            }
        return out

    async def _ask_translations(
        self, anime_id: int, http: httpx.AsyncClient
    ) -> dict[str, Any]:
        """The raw ``/api/player/<id>/translations`` document, or ``{}``
        when the upstream answers in an unexpected shape."""
        doc = await self._get_json(
            f"{BASE_URL}/api/player/{anime_id}/translations", http
        )
        return doc if isinstance(doc, dict) else {}

    async def _collect_episode_map(
        self, anime_id: int, http: httpx.AsyncClient
    ) -> dict[int, list[dict[str, Any]]]:
        """Walk every (translation, player) pair from
        ``/api/player/<id>/translations`` and aggregate per-episode
        source lists. A present-but-empty ``translations`` list is
        deliberate withholding and raises ``gated`` (see
        ``_classify_translations``); a missing/malformed key returns an
        empty map for the caller to surface ``parse_failed``."""
        translations_doc = await self._ask_translations(anime_id, http)
        translations = _classify_translations(translations_doc)

        # Issue #187: the (translation, player) pairs are walked
        # CONCURRENTLY through ONE shared semaphore instead of one-by-
        # one — a long archive needs dozens of upstream pages and the
        # old sequential walk 502'd under throttling. ``_build_season``
        # sorts the aggregated entries by (translation_name,
        # player_name), so completion order never affects the response.
        sem = asyncio.Semaphore(_EPISODE_FETCH_CONCURRENCY)

        async def collect_pair(
            trans: dict[str, Any], player: dict[str, Any]
        ) -> list[dict[str, Any]]:
            trans_obj = trans.get("translation") or {}
            trans_id = trans_obj.get("id")
            trans_name = str(trans_obj.get("name") or "").strip()
            if trans_id is None or not trans_name:
                return []
            return await self._collect_player_sources(
                anime_id, int(trans_id), trans_name, player, http, sem=sem
            )

        pairs = [
            collect_pair(trans, player)
            for trans in translations
            for player in trans.get("player") or []
        ]
        # Issue #192 follow-up: one (translation, player) pair that
        # hiccups (throttle 502, read timeout) must NOT kill the whole
        # series — a long archive has many pairs and a single transient
        # failure would gate the entire card. Failures are logged and
        # the surviving pairs still build the episode map; only when
        # EVERY pair failed do we surface the first real error so the
        # caller sees `upstream_unreachable` / `unreachable` instead of
        # a misleading empty parse.
        results = await asyncio.gather(*pairs, return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        episodes_by_num: dict[int, list[dict[str, Any]]] = {}
        for sources in results:
            if isinstance(sources, BaseException):
                continue
            for entry in sources:
                episodes_by_num.setdefault(int(entry["episode"]), []).append(entry)
        if failures:
            first_error = next(
                (f for f in failures if isinstance(f, ProviderError)), None
            )
            if not episodes_by_num and first_error is not None:
                raise first_error
            logger.warning(
                "animeon %d/%d episode-walk pairs failed (continuing with %d): %s",
                len(failures),
                len(pairs),
                len(episodes_by_num),
                "; ".join(str(f) for f in failures[:3]),
            )
        return episodes_by_num

    async def _collect_player_sources(
        self,
        anime_id: int,
        translation_id: int,
        translation_name: str,
        player: dict[str, Any],
        http: httpx.AsyncClient,
        sem: asyncio.Semaphore | None = None,
    ) -> list[dict[str, Any]]:
        """Walk the paginated ``/api/player/<id>/episodes`` list for
        one (translation, player) pair, deduplicating by episode id.
        The response sometimes wraps specials at ``skip=-1`` (the
        upstream Kotlin calls that out explicitly).

        ``sem`` is the shared walk semaphore from ``_collect_episode_map``
        (a fresh one is created when called standalone, e.g. the movie
        source fallback) so every page fetch is bounded globally."""
        player_id = player.get("id")
        player_name = str(player.get("name") or "").strip()
        if player_id is None or not player_name:
            return []
        episodes_count = int(player.get("episodesCount") or 0)
        max_skip = ((episodes_count // 100) + 1) * 100 if episodes_count > 0 else 11000
        if sem is None:
            sem = asyncio.Semaphore(_EPISODE_FETCH_CONCURRENCY)

        collected: dict[int, dict[str, Any]] = {}
        base = (
            f"{BASE_URL}/api/player/{anime_id}/episodes"
            f"?take=100&playerId={player_id}&translationId={translation_id}"
        )
        # Specials at skip=-1 are optional — a 4xx means the endpoint
        # isn't supported (no specials for this show), which is a valid
        # empty state. A 5xx, on the other hand, means the upstream is
        # actually broken and we must let it bubble up so the caller
        # sees ``upstream_unreachable`` instead of a misleading
        # ``parse_failed`` further down. Held under the semaphore so
        # concurrent pairs don't burst past the bound.
        async with sem:
            specials = await self._fetch_specials_page(base, http)
        if specials is not None:
            self._absorb_collected(
                collected, specials, translation_name, player_name, specials_only=True
            )
        await self._fetch_episode_pages(
            base, max_skip, collected, translation_name, player_name, http, sem=sem
        )
        return list(collected.values())

    async def _fetch_specials_page(
        self, base: str, http: httpx.AsyncClient
    ) -> dict[str, Any] | None:
        """GET ``?skip=-1`` and return the JSON, or None on 4xx (no
        specials for this show). 5xx propagates so the caller surfaces
        ``upstream_unreachable``."""
        try:
            return cast(dict[str, Any], await self._get_json(f"{base}&skip=-1", http))
        except ProviderError as e:
            if e.code in (ProviderErrorCode.UNREACHABLE, ProviderErrorCode.UPSTREAM_UNREACHABLE):
                raise
            logger.debug("animeon skip=-1 specials unavailable: %s", e)
            return None

    def _absorb_collected(
        self,
        collected: dict[int, dict[str, Any]],
        doc: dict[str, Any],
        translation_name: str,
        player_name: str,
        *,
        specials_only: bool,
    ) -> None:
        for ep in (doc or {}).get("episodes") or []:
            if specials_only and int(ep.get("episode") or 0) > 0:
                continue
            ep_id = int(ep.get("id") or 0)
            if ep_id and ep_id not in collected:
                collected[ep_id] = self._build_entry(
                    ep, translation_name, player_name
                )

    async def _fetch_episode_pages(
        self,
        base: str,
        max_skip: int,
        collected: dict[int, dict[str, Any]],
        translation_name: str,
        player_name: str,
        http: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> None:
        """Walk ``/api/player/<id>/episodes?skip=N`` pages from skip=0
        upward until the page is short or empty.

        skip=0 is fetched first (sequential) so a short first page
        stops the walk after exactly one request — the common small-
        series case must not fan out. Only once the first page proves
        the archive is long do the remaining pages go out in bounded-
        concurrency windows (each fetch still acquires ``sem``): a
        1170-episode series needs ~12 pages per pair, and the old
        fully-sequential walk took 10s+ and 502'd under upstream
        throttling (issue #187). A short/empty page still ends the
        walk; offsets beyond it hold nothing worth absorbing."""

        async def fetch_page(skip: int) -> list[dict[str, Any]]:
            async with sem:
                try:
                    doc = await self._get_json(f"{base}&skip={skip}", http)
                except ProviderError as e:
                    if e.code in {"unreachable", "upstream_unreachable"}:
                        raise
                    logger.debug("animeon episodes skip=%d unavailable: %s", skip, e)
                    return []
            return (doc or {}).get("episodes") or []

        first = await fetch_page(0)
        if not first:
            return
        self._absorb_collected(
            collected, {"episodes": first}, translation_name, player_name, specials_only=False
        )
        if len(first) < 100:
            return

        skip = 100
        while skip <= max_skip:
            window = [
                s
                for s in range(skip, skip + _EPISODE_FETCH_CONCURRENCY * 100, 100)
                if s <= max_skip
            ]
            if not window:
                break
            pages = await asyncio.gather(*(fetch_page(s) for s in window))
            for episodes in pages:
                if episodes:
                    self._absorb_collected(
                        collected,
                        {"episodes": episodes},
                        translation_name,
                        player_name,
                        specials_only=False,
                    )
                if len(episodes) < 100:
                    return
            skip = window[-1] + 100

    @staticmethod
    def _build_entry(
        ep: dict[str, Any], translation_name: str, player_name: str
    ) -> dict[str, Any]:
        """Encode one episode row from the player API into the
        internal shape used by ``content()`` and ``stream()``."""
        return {
            "id": int(ep.get("id") or 0),
            "episode": int(ep.get("episode") or 0),
            "video_url": str(ep.get("videoUrl") or "") or None,
            "file_url": str(ep.get("fileUrl") or "") or None,
            "poster": str(ep.get("poster") or "") or None,
            "translation_name": translation_name,
            "player_name": player_name,
        }

    @staticmethod
    def _build_episode(
        anime_id: int,
        episode_num: int,
        entries: list[dict[str, Any]],
        translations: list[str],
        provider_id: str,
        *,
        ep_info: dict[str, Any] | None = None,
    ) -> Episode:
        ep_translations = [
            Translation(id=name, label=name)
            for name in translations
            if any(name == e["translation_name"] for e in entries)
        ]
        # Ticket #223: the ``episodes-info`` row (when the endpoint
        # answered) carries the real Ukrainian title + air date —
        # ``titleUa``/``title`` fall back to the generic "Серія N".
        title = str((ep_info or {}).get("titleUa") or (ep_info or {}).get("title") or "").strip()
        if not title:
            title = f"Серія {episode_num}"
        aired = (ep_info or {}).get("aired")
        premiere_date = str(aired)[:10] if isinstance(aired, str) and aired else None
        # Encode the per-episode source list in Episode.id so
        # stream() can pick one without re-fetching the JSON. The
        # format is a JSON object — short, opaque, ASCII-safe.
        blob = json.dumps(
            {
                "id": anime_id,
                "episode": episode_num,
                "sources": [
                    {
                        "translation_name": e["translation_name"],
                        "player_name": e["player_name"],
                        "video_url": e["video_url"],
                        "file_url": e["file_url"],
                    }
                    for e in entries
                ],
            },
            separators=(",", ":"),
        )
        encoded = base64.b64encode(blob.encode("utf-8")).decode("ascii")
        return Episode(
            number=episode_num,
            id=f"{provider_id}:{anime_id}:e{episode_num}:{encoded}",
            title=title,
            translations=ep_translations,
            premiere_date=premiere_date,
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # Movies stream by their bare id (search-result id, or the
        # explicit `:__movie__` suffix); series episodes carry the
        # encoded `e<N>:<blob>` suffix.
        if ":" in content_id:
            parts = content_id.split(":", 2)
            if len(parts) == 2 and parts[1] == MOVIE_SUFFIX.removeprefix(":"):
                if not _EXTERNAL_ID_RE.fullmatch(parts[0]):
                    raise ProviderError("not_found", "bad content_id")
                return await self._movie_stream(int(parts[0]), translation, http)
            if len(parts) != 3 or not _EXTERNAL_ID_RE.fullmatch(parts[0]):
                raise ProviderError("not_found", "bad content_id")
        else:
            if not _EXTERNAL_ID_RE.fullmatch(content_id):
                raise ProviderError("not_found", "bad content_id")
            return await self._movie_stream(int(content_id), translation, http)
        anime_id = int(parts[0])
        if not re.fullmatch(r"e\d{1,5}", parts[1]):
            raise ProviderError("parse_failed", "bad episode suffix")
        try:
            payload = json.loads(base64.b64decode(parts[2]).decode("utf-8"))
            sources = payload.get("sources", [])
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProviderError("parse_failed", "bad sources blob") from error
        episode_num = int(payload.get("episode") or int(parts[1][1:]))
        if not sources:
            raise ProviderError("parse_failed", "no sources for episode")

        # Match the translation. Fall back to the first available
        # source rather than 404 — the v1 upstream always picks the
        # first file if no translation matches, but we surface
        # `parse_failed` only when the user explicitly named one.
        chosen: dict[str, Any] | None = None
        if translation is not None:
            for src in sources:
                if src.get("translation_name") == translation:
                    chosen = src
                    break
            if chosen is None:
                raise ProviderError(
                    "translation_missing",
                    f"translation {translation!r} not available",
                )
        if chosen is None:
            chosen = sources[0]

        url = await self._resolve_source_url(anime_id, episode_num, chosen, http)
        return StreamResponse(
            url=url,
            type="m3u8",
            headers=_stream_headers(chosen),
        )

    async def _movie_stream(
        self, anime_id: int, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        """Resolve a movie's single stream. Movies have no episode rows
        in `/api/player/<id>/episodes`, so we mirror the upstream
        Kotlin `loadMovieLinks`: try the episode walk per player and
        fall back to the direct endpoint
        `/api/player/<playerId>/<translationId>`, whose `videoUrl`/
        `fileUrl` lead straight to the film."""
        doc = await self._ask_translations(anime_id, http)
        translations = _classify_translations(doc or {})
        for trans in translations:
            t = trans.get("translation") or {}
            name = str(t.get("name") or "").strip()
            trans_id = t.get("id")
            if trans_id is None or not name:
                continue
            if translation is not None and name != translation:
                continue
            for player in trans.get("player") or []:
                player_id = player.get("id")
                player_name = str(player.get("name") or "").strip()
                if player_id is None:
                    continue
                source = await self._resolve_movie_player_source(
                    anime_id, int(trans_id), name, player, player_name, http
                )
                if source is not None:
                    url = await self._resolve_source_url(anime_id, 1, source, http)
                    return StreamResponse(
                        url=url, type="m3u8", headers=_stream_headers(source)
                    )
        if translation is not None:
            raise ProviderError(
                "translation_missing", f"translation {translation!r} not available"
            )
        raise ProviderError("parse_failed", "no movie source resolved")

    async def _resolve_movie_player_source(
        self,
        anime_id: int,
        translation_id: int,
        translation_name: str,
        player: dict[str, Any],
        player_name: str,
        http: httpx.AsyncClient,
    ) -> dict[str, Any] | None:
        """One (translation, player) pair's playable source for a movie:
        the direct player endpoint first (the upstream `loadMovieLinks`
        authoritative source for films — observed live 2026-08-09 the
        episode walk returned a STALE Moon iframe for a movie whose
        direct endpoint resolved fine), then the first episode-row
        entry as a fallback. 4xx on the direct endpoint means "no
        direct source" and is skipped; 5xx propagates as
        `upstream_unreachable`."""
        try:
            direct = await self._get_json(
                f"{BASE_URL}/api/player/{player.get('id')}/{translation_id}", http
            )
        except ProviderError as e:
            if e.code in (ProviderErrorCode.UNREACHABLE, ProviderErrorCode.UPSTREAM_UNREACHABLE):
                raise
            logger.debug("animeon movie direct source unavailable: %s", e)
            direct = None
        if isinstance(direct, dict):
            video_url = str(direct.get("videoUrl") or "") or None
            file_url = str(direct.get("fileUrl") or "") or None
            if video_url or file_url:
                return {
                    "id": 0,
                    "episode": 1,
                    "video_url": video_url,
                    "file_url": file_url,
                    "translation_name": translation_name,
                    "player_name": player_name,
                }
        entries = await self._collect_player_sources(
            anime_id, translation_id, translation_name, player, http
        )
        if entries:
            return entries[0]
        return None

    async def _resolve_source_url(
        self,
        anime_id: int,
        episode_num: int,
        source: dict[str, Any],
        http: httpx.AsyncClient,
    ) -> str:
        """Resolve one EpisodeSource to its final m3u8 URL. Ashdi
        sources usually have ``fileUrl`` already as the manifest;
        if ``fileUrl`` is missing we fall back to scraping the iframe
        page (same regex as the upstream Kotlin's `processAshdiIframe`).
        Moon sources always require the iframe page + XOR decode."""
        file_url = source.get("file_url")
        video_url = source.get("video_url")
        if _is_moon(source):
            if not video_url:
                raise ProviderError("parse_failed", "moon source without video_url")
            return await self._resolve_moon_iframe(video_url, http)
        if _is_ashdi(source):
            if file_url and file_url.endswith(".m3u8"):
                return str(file_url)
            if video_url:
                return await self._resolve_ashdi_iframe(video_url, http)
            # Upstream drift (2026-08-14): the episodes endpoint stopped
            # embedding videoUrl/fileUrl in its rows. The direct endpoint
            # `/api/player/<playerId>/<translationId>` still serves the
            # player page (an ashdi serial page whose Playerjs playlist
            # carries every episode's m3u8) — resolve through it.
            fallback = await self._ashdi_playlist_fallback(
                anime_id, episode_num, source, http
            )
            if fallback is not None:
                return fallback
            raise ProviderError("parse_failed", "ashdi source without urls")
        # Unknown player: prefer fileUrl if it is an m3u8, otherwise
        # try the iframe page's `file:` regex.
        if file_url and ".m3u8" in file_url:
            return str(file_url)
        if video_url:
            return await self._resolve_ashdi_iframe(video_url, http)
        raise ProviderError("parse_failed", "no usable url in source")

    async def _ashdi_playlist_fallback(
        self,
        anime_id: int,
        episode_num: int,
        source: dict[str, Any],
        http: httpx.AsyncClient,
    ) -> str | None:
        """Resolve an episode through the direct player endpoint when the
        episode row carries no urls (upstream drift, 2026-08-14).

        ``/api/player/<playerId>/<translationId>`` answers
        ``{"videoUrl": "https://ashdi.vip/serial/<id>?..."}`` — the ashdi
        serial page whose Playerjs ``file:'[...]'`` value is a JSON
        playlist: translation folders -> season folders -> episode
        entries (``{"title": "Серія N", "file": "...m3u8"}``). Select the
        requested translation's folder (case-insensitive; first folder
        when no name matches, mirroring the upstream's pick-first
        behavior) and the ``Серія <episode_num>`` entry inside it."""
        trans_name = str(source.get("translation_name") or "").strip().casefold()
        player_name = str(source.get("player_name") or "").strip().casefold()
        doc = await self._ask_translations(anime_id, http)
        direct_url: str | None = None
        for trans in _classify_translations(doc or {}):
            t = trans.get("translation") or {}
            if str(t.get("name") or "").strip().casefold() != trans_name:
                continue
            trans_id = t.get("id")
            for player in trans.get("player") or []:
                if str(player.get("name") or "").strip().casefold() != player_name:
                    continue
                player_id = player.get("id")
                if trans_id is None or player_id is None:
                    continue
                direct = await self._get_json(
                    f"{BASE_URL}/api/player/{player_id}/{trans_id}", http
                )
                if isinstance(direct, dict):
                    value = direct.get("videoUrl")
                    if isinstance(value, str) and value:
                        direct_url = value
        if direct_url is None:
            return None
        page = await self._get_text(
            direct_url,
            http,
            headers={"Referer": f"{BASE_URL}/", **_DEFAULT_HEADERS},
        )
        match = re.search(r"file:'((?:[^'\\]|\\.)*)'", page)
        if not match:
            return None
        try:
            raw = re.sub(r"\\'", "'", match.group(1))
            playlist = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(playlist, list):
            return None
        folders = [f for f in playlist if isinstance(f, dict)]
        target = next(
            (
                f
                for f in folders
                if str(f.get("title") or "").strip().casefold() == trans_name
            ),
            None,
        )
        if target is None and folders:
            target = folders[0]
        if target is None:
            return None
        want = f"Серія {episode_num}"
        for season in target.get("folder") or []:
            if not isinstance(season, dict):
                continue
            for entry in season.get("folder") or []:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("title") or "").strip() == want:
                    file_url = entry.get("file")
                    if isinstance(file_url, str) and file_url.endswith(".m3u8"):
                        return file_url
        return None

    async def _resolve_ashdi_iframe(
        self, iframe_url: str, http: httpx.AsyncClient
    ) -> str:
        """Fetch the Ashdi iframe page and extract the ``file:'<m3u8>'``
        value. The upstream Kotlin does the same. We append
        ``?player=animeon.club`` when the URL has no query string,
        otherwise the CDN returns the wrong page."""
        clean = iframe_url.rstrip("?")
        if "?" in clean:
            fetch_url = clean
        else:
            fetch_url = f"{clean}?player=animeon.club"
        page = await self._get_text(
            fetch_url,
            http,
            headers={"Referer": f"{BASE_URL}/", **_DEFAULT_HEADERS},
        )
        match = _ASHDI_FILE_RE.search(page)
        if not match:
            raise ProviderError("parse_failed", "ashdi file: '...' missing")
        return match.group(1)

    async def _resolve_moon_iframe(
        self, iframe_url: str, http: httpx.AsyncClient
    ) -> str:
        """Fetch the MoonAnime iframe page, decode the obfuscated
        Playerjs config, and return the first ``.m3u8`` URL.

        The decrypted payload is either a direct manifest URL or a JSON
        array of tracks (live 2026-08-09: movies — e.g. animeon 8102
        "Ґінтама Фільм 1" — now answer ``[{...,"file":"<m3u8>"}]``;
        the array previously meant the card was dead, today it is the
        current upstream shape).
        """
        clean = iframe_url.rstrip("?")
        if "player=" not in clean:
            separator = "&" if "?" in clean else "?"
            fetch_url = f"{clean}{separator}player=animeon.club"
        else:
            fetch_url = clean
        page = await self._get_text(
            fetch_url,
            http,
            headers={
                **_DEFAULT_HEADERS,
                "Referer": f"{BASE_URL}/",
                "X-Requested-With": "mark.via.gp",
            },
        )

        atob_match = _ATOB_RE.search(page)
        if not atob_match:
            raise ProviderError("parse_failed", "moon atob blob missing")
        decoded_js = _moon_outer_decode(atob_match.group(1)).decode(
            "utf-8", errors="ignore"
        )
        if not decoded_js:
            raise ProviderError("parse_failed", "moon outer decode failed")

        key_match = _KEY_RE.search(decoded_js)
        if not key_match:
            raise ProviderError("parse_failed", "moon xor key missing")
        xor_key = key_match.group(1)

        for inner in _INNER_RE.findall(decoded_js):
            decoded = _moon_decrypt(inner, xor_key).strip().rstrip(",")
            if decoded.startswith("["):
                try:
                    tracks = json.loads(decoded)
                except json.JSONDecodeError:
                    continue
                if isinstance(tracks, list) and not tracks:
                    # A well-formed EMPTY track array is deliberate
                    # upstream unavailability — the movie is listed in
                    # the catalog but moonanime hasn't published the
                    # video yet (live 2026-08-09: animeon 8104
                    # «Літературне дівча Фільм» serves a "Скоро
                    # доступно" placeholder iframe and an empty `[]`
                    # player payload). Per ADR-0002's empty-manifest
                    # amendment this is `gated` (client 404, never a
                    # health signal), NOT `parse_failed` (502, pollutes
                    # the health tracker for a healthy provider).
                    raise ProviderError(
                        "gated", "no playable tracks — video not yet published"
                    )
                for track in tracks if isinstance(tracks, list) else []:
                    url = str(track.get("file") or "").strip()
                    if ".m3u8" in url:
                        return url
                continue
            if ".m3u8" not in decoded:
                continue
            return decoded
        raise ProviderError("parse_failed", "no .m3u8 in moon payload")


def _stream_headers(source: dict[str, Any]) -> dict[str, str]:
    """Headers the m3u8 downloader must carry. Ashdi and Moon CDNs
    both reject plain ``requests`` without the matching Referer —
    upstream Kotlin sets the same constants for `M3u8Helper`."""
    if _is_moon(source):
        return {
            "User-Agent": _DEFAULT_HEADERS["User-Agent"],
            "Referer": MOON_REFERER,
            "Origin": MOON_BASE,
        }
    if _is_ashdi(source):
        return {
            "User-Agent": _DEFAULT_HEADERS["User-Agent"],
            "Referer": ASHDI_REFERER,
            "Origin": "https://ashdi.vip",
        }
    return {}


# Internal alias used in ``_build_local_results`` to avoid an
# unrelated class-method call inside a comprehension.
ANIMEON_ID = AnimeONProvider.id


__all__ = ["AnimeONProvider"]
