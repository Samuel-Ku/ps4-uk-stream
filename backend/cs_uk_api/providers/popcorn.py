"""Popcorn-API shows client — the series upstream of the English lane.

Extracted verbatim from :mod:`yts` (safe refactor): ONE owner for the
Popcorn HTTP conversation, payload normalization and the session
show-cache. The client is a collaborator, NOT a BaseProvider — registry
identity (``id``, sections, the allowlist declaration) stays with the
composing provider, which lends its ``get_json`` fetch path (canonical
error codes + ADR-0005 allowlist enforcement) to every request.

Payload dialect (research #366 §3):

  - list  ``GET {base}/shows/{page}?sort=name&keywords=Q``  (search) or
          ``GET {base}/shows/{page}?sort=updated&order=-1``  (newest)
          → JSON array of show objects (a JSON null is the upstream
          empty-page answer);
  - show  ``GET {base}/show/{imdb_id}`` → one show object:
          ``{imdb_id, title, year: "2019" (STRING), description,
          images: {poster: url|null}, rating: {percentage: 95.0},
          episodes: [{season, episode, title, overview,
          torrents: {quality: {url: magnet, seeds}}}]}``.

Torrents are QUALITY-MAP OBJECTS whose ``url`` magnets are used
VERBATIM (popcorn desktop's tv.js contract) — never hash-rebuilt.

``popcorn_base_url`` settings knob (default ``CS_UK_POPCORN_BASE_URL``)
is read by the composing provider and handed in; every known public
host is dead (research #366) — the knob points at a self-hosted
popcorn-api or live mirror, and an empty base means the client answers
``None``/loud-typed-verdict exactly as the extracted code did.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .base import BaseProvider, ProviderError

#: Listing/search path of the shows API (page-templated).
POPCORN_SHOWS_PATH = "/shows/{page}"

#: One show's details path.
POPCORN_SHOW_PATH = "/show/{imdb_id}"

#: TTL for the in-memory show cache (lean-build session slice): ONE
#: upstream fetch serves every playback-surface call — PlaybackInfo,
#: stream, VTT — for one viewing session. Episode ``stream()`` needs
#: the season torrent map again on EVERY facade call, so without this
#: each of the three calls re-hits Popcorn and playback dies on any
#: upstream blip mid-session. 5 min: a season's torrent set changes
#: on a scale of hours, so staleness is immaterial beside liveness.
SHOW_CACHE_TTL_S = 300.0

#: LRU bound for the same cache (a long-tail browse must not pin
#: unbounded show objects).
SHOW_CACHE_MAX = 64


# ---------------------------------------------------------------------------
# Payload guards + field normalization (pure, no I/O)
# ---------------------------------------------------------------------------


def _require_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderError("parse_failed", f"{what} not an object")
    return value


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("parse_failed", f"{what} missing")
    return value


def _shows_of_page(payload: Any) -> list[dict[str, Any]]:
    """The shows array of a Popcorn ``/shows/{page}`` envelope.

    A JSON null (the upstream empty-page answer) is a legitimate empty
    listing → ``[]``; anything else malformed raises typed
    ``parse_failed``.
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ProviderError("parse_failed", "shows page not a list")
    return [s for s in payload if isinstance(s, dict)]


def _show_of_details(payload: Any) -> dict[str, Any]:
    """The show object of a Popcorn ``/show/{imdb_id}`` envelope."""
    return _require_object(payload, "show object")


def _episodes_of_show(show: dict[str, Any]) -> list[dict[str, Any]]:
    episodes = show.get("episodes")
    if not isinstance(episodes, list):
        raise ProviderError("parse_failed", "episodes not a list")
    return [e for e in episodes if isinstance(e, dict)]


def _episode_title(episode: dict[str, Any], fallback: str) -> str:
    title = episode.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return fallback


def _episode_overview(episode: dict[str, Any]) -> str:
    overview = episode.get("overview")
    return overview if isinstance(overview, str) else ""


def _show_poster_of(show: dict[str, Any]) -> str | None:
    """The Popcorn poster: nested ``images.poster`` (a null when absent)."""
    images = show.get("images")
    if not isinstance(images, dict):
        return None
    poster = images.get("poster")
    return poster if isinstance(poster, str) and poster else None


def _show_year_of(show: dict[str, Any]) -> int | None:
    """Popcorn ships ``year`` as a STRING ("2019"); YTS ships an int.
    One coercion the series pass uses so both dialects land as ints."""
    year = show.get("year")
    if isinstance(year, bool):
        return None
    if isinstance(year, int):
        return year
    if isinstance(year, str) and year.isdigit() and len(year) == 4:
        return int(year)
    return None


def _show_rating_of(show: dict[str, Any]) -> float | None:
    """Popcorn ships ``rating`` as ``{"percentage": 95.0, ...}`` (the
    YTS movie shape is a bare number); the series pass reads the
    percentage, the same 0-100 scale the movie lane reports."""
    rating = show.get("rating")
    if isinstance(rating, dict):
        pct = rating.get("percentage")
        if isinstance(pct, int | float) and not isinstance(pct, bool):
            return float(pct)
        return None
    if isinstance(rating, int | float) and not isinstance(rating, bool):
        return float(rating)
    return None


# ---------------------------------------------------------------------------
# The conversation: one client per composing provider
# ---------------------------------------------------------------------------


class PopcornShows:
    """The Popcorn shows conversation for one composing provider.

    Owns the configured base, the URL grammar, the payload
    normalization boundary and the session show-cache. Fetches ride the
    composing provider's ``get_json`` so error codes, the redirect
    allowlist and health-tracking stay single-sourced.
    """

    def __init__(self, provider: BaseProvider, base: str | None) -> None:
        self._provider = provider
        #: The configured base, normalized exactly as the extracted
        #: provider field was (strip; empty ⇒ series pass off).
        self.base = (base or "").strip()
        #: imdb → (monotonic_ts, show) — one Popcorn fetch per TTL
        #: window per show (see SHOW_CACHE_TTL_S).
        self._cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    @property
    def netloc(self) -> str:
        """The host the composing provider must allowlist (ADR-0005)."""
        return urlparse(self.base).netloc

    def require_base(self) -> str:
        """The configured series base, or a LOUD typed verdict.

        The series pass is deliberately NOT a silent empty listing when
        unconfigured — the operator must see that the lane is off (the
        same not-configured posture the engine knob set in #377).
        """
        if not self.base:
            raise ProviderError(
                "unreachable",
                "popcorn series host not configured (CS_UK_POPCORN_BASE_URL)",
            )
        return self.base

    def show_url(self, imdb: str) -> str:
        """The human/upstream details URL of one show (card ``url``)."""
        return f"{self.base}{POPCORN_SHOW_PATH.format(imdb_id=imdb)}"

    async def _get_shows(
        self, path: str, query: str, http: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        base = self.require_base()
        payload = await self._provider.get_json(f"{base}{path}?{query}", http)
        return _shows_of_page(payload)

    async def search_shows(
        self, query: str, http: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        """The shows page of a search query (page 1 suffices for a
        search box; deep pagination is a browse concern)."""
        return await self._get_shows(
            POPCORN_SHOWS_PATH.format(page=1),
            f"sort=name&keywords={quote(query, safe='')}",
            http,
        )

    async def updated_shows(
        self, page: int, http: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        """One shows page, newest-updated first."""
        return await self._get_shows(
            POPCORN_SHOWS_PATH.format(page=page), "sort=updated&order=-1", http
        )

    async def show(self, imdb: str, http: httpx.AsyncClient) -> dict[str, Any] | None:
        """The show object for ``imdb``, or ``None`` when the series
        host is not configured (the movies-first envelope falls out
        instead — typed verdicts are the SERIES-only paths' job).

        Cached for ``SHOW_CACHE_TTL_S``: one upstream fetch serves the
        whole PlaybackInfo → stream → VTT call chain (acceptance: the
        in-TTL chain must survive an upstream outage)."""
        if not self.base:
            return None
        now = time.monotonic()
        cached = self._cache.get(imdb)
        if cached is not None and now - cached[0] < SHOW_CACHE_TTL_S:
            self._cache.move_to_end(imdb)
            return cached[1]
        payload = await self._provider.get_json(
            f"{self.base}{POPCORN_SHOW_PATH.format(imdb_id=imdb)}", http
        )
        show = _show_of_details(payload)
        self._cache[imdb] = (now, show)
        self._cache.move_to_end(imdb)
        while len(self._cache) > SHOW_CACHE_MAX:
            self._cache.popitem(last=False)
        return show
