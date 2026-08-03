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
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, MediaTypeStr, ProviderError

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
    Section(id="seasons", title="Сезон", type="anime"),
    Section(id="popular", title="Популярні", type="anime"),
    Section(id="page", title="Нове аніме", type="anime"),
)

# Bare integer ids only (matches the v1 Kotlin's
# substringAfterLast("/").substringBefore("-").toIntOrNull()).
_EXTERNAL_ID_RE = re.compile(r"\d{1,8}")

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
    return datetime.now().strftime("%a %b %d %Y").replace(" 0", "  ")


def _poster_url(preview: str | None) -> str | None:
    """Build the canonical poster URL from the API's ``image.preview``
    filename — the upstream posterApi template is
    ``$mainUrl/api/uploads/images/<preview>``."""
    if not preview:
        return None
    return f"{BASE_URL}/api/uploads/images/{preview}"


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
        return [
            SearchResult(
                id=f"{self.id}:{item['id']}",
                provider=self.id,
                type="anime",
                title=str(item.get("titleUa", "")).strip(),
                poster=_poster_url((item.get("image") or {}).get("preview")),
                url=f"{BASE_URL}/anime/{item['id']}",
            )
            for item in results
            if "id" in item and str(item.get("titleUa") or "").strip()
        ]

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
            out.append(
                SearchResult(
                    id=f"{ANIMEON_ID}:{item['id']}",
                    provider="animeon",
                    type="anime",
                    title=str(item.get("titleUa", "")).strip(),
                    poster=_poster_url(image.get("preview")),
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

        episodes_by_num = await self._collect_episode_map(anime_id, http)
        if not episodes_by_num:
            raise ProviderError("parse_failed", "no episodes resolved")

        all_translations = sorted(
            {
                str(entry["translation_name"])
                for entries in episodes_by_num.values()
                for entry in entries
            }
        )
        season = self._build_season(anime_id, episodes_by_num, all_translations)
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            type="anime",
            title=title,
            year=year_int,
            description=description,
            poster=poster,
            translations=[
                Translation(id=name, label=name) for name in all_translations
            ],
            seasons=[season],
            translations_level="episode",
        )

    @staticmethod
    def _build_season(
        anime_id: int,
        episodes_by_num: dict[int, list[dict[str, Any]]],
        all_translations: list[str],
    ) -> Season:
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
                )
                for ep_num, entries in sorted(episodes_by_num.items())
            ],
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
            try:
                year_int = datetime.strptime(raw_year[:10], "%Y-%m-%d").year
            except ValueError:
                year_int = None
        return info, year_int

    async def _collect_episode_map(
        self, anime_id: int, http: httpx.AsyncClient
    ) -> dict[int, list[dict[str, Any]]]:
        """Walk every (translation, player) pair from
        ``/api/player/<id>/translations`` and aggregate per-episode
        source lists. Returns an empty dict when the API returns no
        translations — the caller surfaces ``parse_failed``."""
        translations_doc = await self._get_json(
            f"{BASE_URL}/api/player/{anime_id}/translations", http
        )
        translations: list[dict[str, Any]] = []
        if isinstance(translations_doc, dict):
            raw = translations_doc.get("translations")
            if isinstance(raw, list):
                translations = raw

        episodes_by_num: dict[int, list[dict[str, Any]]] = {}
        for trans in translations:
            trans_obj = trans.get("translation") or {}
            trans_id = trans_obj.get("id")
            trans_name = str(trans_obj.get("name") or "").strip()
            if trans_id is None or not trans_name:
                continue
            for player in trans.get("player") or []:
                sources = await self._collect_player_sources(
                    anime_id, int(trans_id), trans_name, player, http
                )
                for entry in sources:
                    episodes_by_num.setdefault(int(entry["episode"]), []).append(entry)
        return episodes_by_num

    async def _collect_player_sources(
        self,
        anime_id: int,
        translation_id: int,
        translation_name: str,
        player: dict[str, Any],
        http: httpx.AsyncClient,
    ) -> list[dict[str, Any]]:
        """Walk the paginated ``/api/player/<id>/episodes`` list for
        one (translation, player) pair, deduplicating by episode id.
        The response sometimes wraps specials at ``skip=-1`` (the
        upstream Kotlin calls that out explicitly)."""
        player_id = player.get("id")
        player_name = str(player.get("name") or "").strip()
        if player_id is None or not player_name:
            return []
        episodes_count = int(player.get("episodesCount") or 0)
        max_skip = ((episodes_count // 100) + 1) * 100 if episodes_count > 0 else 11000

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
        # ``parse_failed`` further down.
        specials = await self._fetch_specials_page(base, http)
        if specials is not None:
            self._absorb_collected(
                collected, specials, translation_name, player_name, specials_only=True
            )
        await self._fetch_episode_pages(
            base, max_skip, collected, translation_name, player_name, http
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
            if e.code in {"unreachable", "upstream_unreachable"}:
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
    ) -> None:
        """Walk ``/api/player/<id>/episodes?skip=N`` pages from skip=0
        upward until the page is short or empty."""
        skip = 0
        while skip <= max_skip:
            try:
                doc = await self._get_json(f"{base}&skip={skip}", http)
            except ProviderError as e:
                if e.code in {"unreachable", "upstream_unreachable"}:
                    raise
                logger.debug("animeon episodes skip=%d unavailable: %s", skip, e)
                break
            episodes = (doc or {}).get("episodes") or []
            if not episodes:
                break
            self._absorb_collected(
                collected, doc, translation_name, player_name, specials_only=False
            )
            if len(episodes) < 100:
                break
            skip += 100

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
    ) -> Episode:
        ep_translations = [
            Translation(id=name, label=name)
            for name in translations
            if any(name == e["translation_name"] for e in entries)
        ]
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
            id=f"{anime_id}:e{episode_num}:{encoded}",
            title=f"Серія {episode_num}",
            translations=ep_translations,
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        parts = content_id.split(":", 2)
        if len(parts) != 3 or not _EXTERNAL_ID_RE.fullmatch(parts[0]):
            raise ProviderError("not_found", "bad content_id")
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
            raise ProviderError("parse_failed", "ashdi source without urls")
        # Unknown player: prefer fileUrl if it is an m3u8, otherwise
        # try the iframe page's `file:` regex.
        if file_url and ".m3u8" in file_url:
            return str(file_url)
        if video_url:
            return await self._resolve_ashdi_iframe(video_url, http)
        raise ProviderError("parse_failed", "no usable url in source")

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
        Playerjs config, and return the first ``.m3u8`` URL."""
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
            decoded = _moon_decrypt(inner, xor_key)
            if ".m3u8" in decoded:
                return decoded.strip().rstrip(",")
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
