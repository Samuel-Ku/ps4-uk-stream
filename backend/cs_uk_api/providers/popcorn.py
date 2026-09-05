"""Popcorn-API conversation — the English lane's ONE upstream client.

ONE owner for the Popcorn HTTP conversation, payload normalization and
the session show-cache — BOTH dialects of the upstream family
(deepening: the series conversation extracted in #387, the movie
conversation moved out of :mod:`yts` in the same pass):

  - MOVIES (the YTS v2 JSON dialect, ``movie_base`` — the composing
    provider's YTS base, e.g. ``https://yts.gg/api/v2/``):
    ``list_movies.json`` (search/browse listings) and
    ``movie_details.json``, both carrying ``torrents[]`` entries;
  - SHOWS (the Popcorn-API series dialect, ``base`` — the configured
    ``CS_UK_POPCORN_BASE_URL`` host): ``/shows/{page}`` and
    ``/show/{imdb_id}`` with per-episode quality maps.

The client is a collaborator, NOT a BaseProvider — registry identity
(``id``, sections, the allowlist declaration) stays with the composing
provider, which lends its ``get_json`` fetch path (canonical error
codes + ADR-0005 allowlist enforcement) to every request.

Payload dialects:

  - list   ``GET {movie_base}/api/v2/list_movies.json?…``
          → ``{data: {movies: [...], movie_count, limit}}`` — a
          status-ok envelope without ``movies`` is the upstream's
          legitimate no-match answer → ``[]``;
  - movie  ``GET {movie_base}/api/v2/movie_details.json?imdb_id=…``
          → ``{data: {movie: {imdb_code, title_english, year: 2019
          (INT), genres, medium_cover_image, rating: 8.2,
          description_full, torrents: [{quality, hash, seeds}]}}}``;
  - shows  ``GET {base}/shows/{page}?sort=name&keywords=Q``  (search)
          or ``GET {base}/shows/{page}?sort=updated&order=-1``  (newest)
          → JSON array of show objects (a JSON null is the upstream
          empty-page answer);
  - show   ``GET {base}/show/{imdb_id}`` → one show object:
          ``{imdb_id, title, year: "2019" (STRING), description,
          images: {poster: url|null}, rating: {percentage: 95.0},
          episodes: [{season, episode, title, overview,
          torrents: {quality: {url: magnet, seeds}}}]}``.

Movie torrents are HASH entries (``hash``) the policy rebuilds into
magnets; show torrents are QUALITY-MAP OBJECTS whose ``url`` magnets
are used VERBATIM (popcorn desktop's tv.js contract) — never
hash-rebuilt. Torrent CANDIDATES stay policy-shaped and live in the
composing provider; this module hands back raw entries inside
:class:`PopcornMovie`.

``popcorn_base_url`` settings knob (default ``CS_UK_POPCORN_BASE_URL``)
is read by the composing provider and handed in; every known public
host is dead (research #366) — the knob points at a self-hosted
popcorn-api or live mirror, and an empty base means the client answers
``None``/loud-typed-verdict exactly as the extracted code did.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from .base import BaseProvider, ProviderError

#: Listing/search path of the shows API (page-templated).
POPCORN_SHOWS_PATH = "/shows/{page}"

#: One show's details path.
POPCORN_SHOW_PATH = "/show/{imdb_id}"

#: The YTS v2 movie-dialect paths (list + details).
MOVIE_LIST_PATH = "/api/v2/list_movies.json"
MOVIE_DETAILS_PATH = "/api/v2/movie_details.json"

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


@dataclass(frozen=True)
class PopcornMovie:
    """The normalized movie-dialect record across the conversation seam.

    ``torrents`` are the RAW ``torrents[]`` entries (quality/hash/seeds
    dicts) — candidate shaping is the policy's job, in the composing
    provider.
    """

    imdb: str | None
    title: str
    year: int | None
    genres: list[str]
    poster: str | None
    rating: float | None
    description: str
    torrents: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MovieListing:
    """One ``list_movies.json`` envelope, typed across the seam."""

    movies: list[PopcornMovie]
    movie_count: int | None
    limit: int | None


# ---------------------------------------------------------------------------
# Payload guards + field normalization (pure, no I/O) — shared by BOTH
# dialects, so a payload-shape change touches exactly one file.
# ---------------------------------------------------------------------------


def _require_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderError("parse_failed", f"{what} not an object")
    return value


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("parse_failed", f"{what} missing")
    return value


def _display_title(movie: dict[str, Any]) -> str:
    title = movie.get("title_english") or movie.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ProviderError("parse_failed", "title missing")
    return title


def _year_of(movie: dict[str, Any]) -> int | None:
    year = movie.get("year")
    return year if isinstance(year, int) else None


def _genres_of(movie: dict[str, Any]) -> list[str]:
    genres = movie.get("genres")
    if not isinstance(genres, list):
        return []
    return [g for g in genres if isinstance(g, str) and g]


def _poster_of(movie: dict[str, Any]) -> str | None:
    poster = movie.get("medium_cover_image")
    return poster if isinstance(poster, str) and poster else None


def _rating_of(movie: dict[str, Any]) -> float | None:
    rating = movie.get("rating")
    return float(rating) if isinstance(rating, int | float) else None


def _description_of(movie: dict[str, Any]) -> str:
    for key in ("description_full", "synopsis", "summary"):
        text = movie.get(key)
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _torrent_entries_of(movie: dict[str, Any]) -> list[dict[str, Any]]:
    torrents = movie.get("torrents")
    if not isinstance(torrents, list):
        return []
    return [t for t in torrents if isinstance(t, dict)]


def _movies_of_list(payload: Any) -> list[dict[str, Any]]:
    """The movies array of a list_movies envelope.

    A status-ok envelope with NO ``movies`` key is upstream's legitimate
    no-match answer → ``[]`` (an empty listing is never a parse failure);
    anything else malformed raises typed ``parse_failed``.
    """
    data = _require_object(_require_object(payload, "payload").get("data"), "data")
    movies = data.get("movies")
    if movies is None:
        return []
    if not isinstance(movies, list):
        raise ProviderError("parse_failed", "movies not a list")
    return [m for m in movies if isinstance(m, dict)]


def _movie_of_details(payload: Any) -> dict[str, Any]:
    """The singular movie object of a movie_details envelope."""
    data = _require_object(_require_object(payload, "payload").get("data"), "data")
    movie = data.get("movie")
    return _require_object(movie, "movie object")


def _movie_record(movie: dict[str, Any]) -> PopcornMovie:
    """A raw movie object → the typed conversation record."""
    imdb = movie.get("imdb_code")
    return PopcornMovie(
        imdb=imdb if isinstance(imdb, str) else None,
        title=_display_title(movie),
        year=_year_of(movie),
        genres=_genres_of(movie),
        poster=_poster_of(movie),
        rating=_rating_of(movie),
        description=_description_of(movie),
        torrents=_torrent_entries_of(movie),
    )


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


class PopcornApi:
    """The Popcorn conversation for one composing provider.

    Owns the configured bases (the movie dialect's YTS base + the
    series dialect's popcorn base), the URL grammar of both dialects,
    the payload normalization boundary and the session show-cache.
    Fetches ride the composing provider's ``get_json`` so error codes,
    the redirect allowlist and health-tracking stay single-sourced.
    """

    def __init__(
        self,
        provider: BaseProvider,
        base: str | None,
        movie_base: str | None = None,
    ) -> None:
        self._provider = provider
        #: The configured series base, normalized exactly as the
        #: extracted provider field was (strip; empty ⇒ series pass off).
        self.base = (base or "").strip()
        #: The movie dialect's base (the composing provider's YTS host);
        #: required for the movie methods.
        self.movie_base = (movie_base or "").strip()
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

    def require_movie_base(self) -> str:
        """The movie dialect's base, or a LOUD typed verdict."""
        if not self.movie_base:
            raise ProviderError("unreachable", "popcorn movie host not configured")
        return self.movie_base

    def show_url(self, imdb: str) -> str:
        """The human/upstream details URL of one show (card ``url``)."""
        return f"{self.base}{POPCORN_SHOW_PATH.format(imdb_id=imdb)}"

    def movie_url(self, imdb: str) -> str:
        """The upstream details URL of one movie (card ``url``)."""
        return f"{self.require_movie_base()}{MOVIE_DETAILS_PATH}?{urlencode({'imdb_id': imdb})}"

    # -- shows dialect --------------------------------------------------

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

    # -- movies dialect --------------------------------------------------

    async def movies(
        self, params: dict[str, str], http: httpx.AsyncClient
    ) -> MovieListing:
        """One ``list_movies.json`` page (search or browse params).

        Pagination numbers ride along: ``movie_count``/``limit`` are
        None when absent, and the composing provider decides how to
        interpret them (has-next is a listing policy, not a parse).
        """
        payload = await self._provider.get_json(
            f"{self.require_movie_base()}{MOVIE_LIST_PATH}?{urlencode(params)}", http
        )
        data = _require_object(_require_object(payload, "payload").get("data"), "data")
        count = data.get("movie_count")
        limit = data.get("limit")
        return MovieListing(
            movies=[_movie_record(m) for m in _movies_of_list(payload)],
            movie_count=count if isinstance(count, int) else None,
            limit=limit if isinstance(limit, int) else None,
        )

    async def movie(self, imdb: str, http: httpx.AsyncClient) -> PopcornMovie:
        """The typed movie record of one ``movie_details.json`` call."""
        payload = await self._provider.get_json(
            f"{self.require_movie_base()}{MOVIE_DETAILS_PATH}?{urlencode({'imdb_id': imdb})}",
            http,
        )
        return _movie_record(_movie_of_details(payload))