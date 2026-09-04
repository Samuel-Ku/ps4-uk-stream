"""YTS provider — the original-English catalog lane (spec #374, tickets
#376 movies + #379 series).

The upstream is the YTS v2 JSON API (`https://yts.gg/api/v2/`,
verified live 2026-08-25; response banner announces the base moving to
``https://movies-api.accel.li/api/v2/``). Both hosts are declared in
``allowed_hosts`` so ``safe_get`` admits a redirect hop during the
in-flight migration; no failover loop beyond that — minimum surface.

Movies carry torrents[] embedded in both list and details responses.
SERIES (#379): YTS ships no series catalogue — research #366 found the
Popcorn-API SERIES contract (per-episode quality maps keyed by quality
name, ``magnet:?`` ``url``s used VERBATIM, no hash rebuilding) the only
public grammar, with every known clearnet host dead or parked. The
``popcorn_base_url`` settings knob (default ``CS_UK_POPCORN_BASE_URL``)
aims the series pass at a configured host — a self-hosted popcorn-api
on the LAN host or any live mirror — via the SAME ``safe_get`` allowlist
(centerally enforced, hosts declared here). Unset ⇒ the movies lane
stays fully live and the series pass answers a LOUD typed verdict, the
same not-configured posture the engine knob set (#377).

Wire identity: the external id IS the IMDb code (``tt1160419``) —
stable across upstream listing churn, which resume/user-state depend
on (spec #374 decision). Display title is ``title_english`` (movies) /
``title`` (shows); YTS/Popcorn ``language`` is display metadata only,
items are mapped as listed.

Envelope duality on one external id (the wire grammar is unambiguous):
  - ``yts:<imdb>:__movie__`` → :meth:`YtsProvider.stream` plays the
    best-seeded 1080p/720p movie torrent (policy in ticket #377).
  - ``yts:<imdb>:s<N>e<M>`` → episode file selection: the same policy
    picks one torrent for the season, then the engine's ``file_hint``
    picks the right FILE inside a multi-file season pack (name-match,
    BitPlayClient._select_file). The parsed season is the season
    number — no second season discriminator on the wire.

Torrent candidates are threaded onto the provider instance (LRU-
bounded; :meth:`YtsProvider.torrent_hashes` is the compat quality→hash
view) so playback can build magnets without a second upstream call.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from .. import config as _config
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from ..torrent_engine import (
    EngineRejected,
    EngineStream,
    EngineUnavailable,
    TorrentEngine,
    get_engine,
)
from ..wire_identity import (
    MOVIE_SUFFIX,
    episode_wire_id,
    is_movie_wire_id,
    parse_episode_tail,
    split_episode_tail,
    strip_movie_suffix,
)
from .base import BaseProvider, ProviderError

BASE_URL = "https://yts.gg"
#: Migration base announced by the API banner (research #366 §5) —
#: declared so an allowlist-followed redirect keeps working.
MIRROR_BASE_URL = "https://movies-api.accel.li"

_LIST_PATH = "/api/v2/list_movies.json"
_DETAILS_PATH = "/api/v2/movie_details.json"

#: Popcorn-API SERIES grammar (research #366 §3) — the pass configured
#: by ``popcorn_base_url``. ``CS_UK_POPCORN_BASE_URL`` env override kept
#: literal per module contract (imports stay stdlib + models only).
POPCORN_SHOWS_PATH = "/shows/{page}"

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
_SHOW_CACHE_MAX = 64
POPCORN_SHOW_PATH = "/show/{imdb_id}"

_SECTIONS = (
    Section(id="movies", title="Фільми", form="movie"),
    Section(id="series", title="Серіали", form="series"),
)

log = logging.getLogger(__name__)

#: Boundary validation: only a well-formed IMDb code may reach the URL.
_IMDB_RE = re.compile(r"tt\d{7,8}")

#: Episode wire ids carry a season/episode tail; playable-id parsing
#: rides the canonical grammar helpers (no second regex here).

_LIST_LIMIT = 50

#: Season-pack file naming (release convention): ``s01e01``-style zero-
#: padded season token, the substring the season file-hint matches
#: inside a multi-file torrent.
_SEASON_HINT_FMT = "s{:02d}e"

# ---------------------------------------------------------------------------
# Magnet→session selection policy (#377) — PURE functions, no I/O.
#
# Spec #374 quality policy: the provider picks the best-seeded suitable
# quality server-side; ONE magnet goes to the engine, there is no
# fallback chain. Ordering key: (quality tier, seeds desc) — 1080p
# preferred over 720p over everything else; within a tier more seeders
# win; full ties keep upstream order (deterministic single pick).
# ---------------------------------------------------------------------------

#: Quality tiers in preference order; anything unlisted shares the last
#: tier (2160p is deliberately NOT preferred in v1 — the player floor
#: this lane targets is 1080p).
_QUALITY_PREFERENCE: tuple[str, ...] = ("1080p", "720p")

#: Tracker list convention of the fork, appended to every built magnet:
#: YTS hashes alone carry no announce URLs, so the engine's peer
#: discovery needs public tracker fallbacks alongside its DHT bootstrap
#: (same convention as the fork's own magnets / research #367 probe).
TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
)


@dataclass(frozen=True)
class TorrentCandidate:
    """One usable ``torrents[]`` entry: quality + info-hash + swarm."""

    quality: str
    info_hash: str
    seeds: int


def _quality_rank(quality: str) -> int:
    try:
        return _QUALITY_PREFERENCE.index(quality)
    except ValueError:
        return len(_QUALITY_PREFERENCE)


def select_torrent(candidates: list[TorrentCandidate]) -> TorrentCandidate | None:
    """The single server-side pick under the decided policy.

    ``min`` is stable, so equal (tier, seeds) keys keep upstream's first
    listing — the deterministic tiebreak. ``None`` when nothing usable.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda c: (_quality_rank(c.quality), -c.seeds))


def build_magnet(info_hash: str) -> str:
    """``magnet:?xt=urn:btih:<hash>&tr=…`` with the fork's trackers."""
    tr = "&".join(f"tr={quote(t, safe='')}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&{tr}"


def _torrent_stream_response(result: EngineStream) -> StreamResponse:
    """EngineStream → wire StreamResponse (#378).

    The engine is the TRUTH about what the file carries: its VTT
    subtitle endpoint rides along verbatim; an empty session (no srt)
    maps to the omitted (None) field — the wire stays byte-identical
    to the pre-#378 shape.
    """
    return StreamResponse(
        url=result.url,
        type="mp4",
        headers={},
        seekable=result.seekable,
        subtitle_url=result.subtitle_url,
    )


def _parse_playable_id(content_id: str) -> tuple[str | None, int | None]:
    """A playable YTS wire id → ``(imdb, season | None)``.

    The wire grammar (module docstring):
      - ``yts:<imdb>:__movie__`` → ``(imdb, None)`` — the movie sentinel
        (#376/#377 contract, unchanged);
      - ``yts:<imdb>:s<N>e<M>`` → ``(imdb, N)`` — the #379 episode form;
        the parsed season number IS the season discriminator (nothing
        else rides the tail);
      - anything else — garbage, a bare id (not playable), a ``g2:``
        key, or a malformed tail — → ``(None, None)``.
    """
    if is_movie_wire_id(content_id):
        stripped = strip_movie_suffix(content_id)
        # The id arrives with or without the ``yts:`` prefix (the native
        # route strips it; episode ids keep it) — accept both spellings.
        imdb = stripped.partition(":")[2] if stripped.startswith("yts:") else stripped
        return imdb, None
    split = split_episode_tail(content_id)
    if split is None:
        return None, None
    composite, tail = split
    # Both spellings arrive (the facade's episode-wire resolution strips
    # the provider prefix — the ``tt8740758:s1e1`` form the native route
    # hands over; provider-scoped ids keep it). Accept both: a leading
    # ``yts:`` is stripped, anything else foreign is refused.
    composite = composite.removeprefix("yts:")
    if ":" in composite or not _IMDB_RE.fullmatch(composite):
        return None, None
    parsed = parse_episode_tail(tail)
    if parsed is None:
        return None, None
    season, _episode = parsed
    return composite, season


def _torrent_candidates(movie: dict[str, Any]) -> list[TorrentCandidate]:
    """Every usable torrents[] entry of a payload, upstream order kept."""
    torrents = movie.get("torrents")
    if not isinstance(torrents, list):
        return []
    out: list[TorrentCandidate] = []
    for entry in torrents:
        if not isinstance(entry, dict):
            continue
        quality = entry.get("quality")
        info_hash = entry.get("hash")
        seeds = entry.get("seeds")
        if not (isinstance(quality, str) and quality and isinstance(info_hash, str) and info_hash):
            continue
        out.append(
            TorrentCandidate(
                quality=quality,
                info_hash=info_hash,
                seeds=seeds if isinstance(seeds, int) and not isinstance(seeds, bool) else 0,
            )
        )
    return out


def _require_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderError("parse_failed", f"{what} not an object")
    return value


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("parse_failed", f"{what} missing")
    return value


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


# ---------------------------------------------------------------------------
# Popcorn-API SERIES parsing (#379) — the grammar research #366 pinned:
#   list  GET {base}/shows/{page}?sort=updated&order=-1
#         → [{"_id","imdb_id","title","year","images":{"poster"},"genres"}, ...]
#   show  GET {base}/show/{imdb_id}
#         → {..., "episodes": [{"season":N,"episode":M,"title","overview",
#              "torrents": {"720p": {"url": "magnet:?...", "seeds": 45,
#                                    "provider": "EZTV"}, ...}}]}
# Torrents are QUALITY-MAP OBJECTS whose ``url`` magnets are used
# VERBATIM (popcorn desktop's tv.js contract) — never hash-rebuilt.
# ---------------------------------------------------------------------------


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


def _show_poster_of(show: dict[str, Any]) -> str | None:
    """The Popcorn poster: nested ``images.poster`` (a null when absent)."""
    images = show.get("images")
    if not isinstance(images, dict):
        return None
    poster = images.get("poster")
    return poster if isinstance(poster, str) and poster else None


def _rating_of(movie: dict[str, Any]) -> float | None:
    rating = movie.get("rating")
    return float(rating) if isinstance(rating, int | float) else None


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
    return _rating_of(show)


def _description_of(movie: dict[str, Any]) -> str:
    for key in ("description_full", "synopsis", "summary"):
        text = movie.get(key)
        if isinstance(text, str) and text.strip():
            return text
    return ""


#: LRU bound on recorded torrent candidates (review finding, #377):
#: search/browse/content thread entries for every parsed item, so an
#: unbounded per-instance map would grow with the catalog over process
#: lifetime. OrderedDict move-to-end-on-access; oldest id evicted.
_TORRENT_ENTRIES_LIMIT = 512


class YtsProvider(BaseProvider):
    id = "yts"
    name = "YTS"
    types = ("movie", "series")
    sections = _SECTIONS
    #: Home composition: newest = page-N ``sort_by=date_added`` listing
    #: (spec #263 recent rows read this once registered). The movies
    #: section stays THE newest feed — the series listing is newer-churn
    #: but unranked by the YTS API, so it does not compete for the row
    #: (#380's composition pins ride the movies page).
    newest_section = "movies"
    #: The YTS API base + its announced migration mirror (the movie
    #: lane) PLUS the configured Popcorn-API series host — every fetch
    #: AND redirect hop is checked against this declaration (ADR-0005).
    #: The series host is user-configured, so the declaration is an
    #: instance attribute seeded from the settings knob (class default
    #: covers only the pinned YTS hosts).
    allowed_hosts = frozenset({"yts.gg", "movies-api.accel.li"})

    def __init__(self, engine: TorrentEngine | None = None) -> None:
        #: Constructor injection of the engine (the uakino session
        #: precedent): tests inject :class:`FakeTorrentEngine`; when
        #: unset, stream-time consults the lazy settings-backed
        #: singleton (:func:`cs_uk_api.torrent_engine.get_engine`).
        self._engine = engine
        #: imdb_code → torrent candidates (quality/hash/seeds), LRU-
        #: bounded; refreshed by every parsed list/details payload
        #: carrying torrents[]. Instance state on the subclass
        #: (BaseProvider contract untouched); consumed by playback.
        self._torrent_entries: OrderedDict[str, list[TorrentCandidate]] = OrderedDict()
        #: The configured Popcorn-API series base (movies pass is
        #: independent of it). Empty ⇒ series surfaces the LOUD typed
        #: not-configured verdict while movies stay fully live.
        self._popcorn_base = (_config.SETTINGS.popcorn_base_url or "").strip()
        #: imdb → (monotonic_ts, show) — one Popcorn fetch per TTL
        #: window per show (see SHOW_CACHE_TTL_S). Instance state on
        #: the subclass; the provider is the single owner of its
        #: upstream conversation.
        self._show_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        if self._popcorn_base:
            self.allowed_hosts = frozenset(
                {*type(self).allowed_hosts, urlparse(self._popcorn_base).netloc}
            )

    def _require_popcorn(self) -> str:
        """The configured series base, or a LOUD typed verdict.

        The series pass is deliberately NOT a silent empty listing when
        unconfigured — the operator must see that the lane is off (the
        same not-configured posture the engine knob set in #377).
        """
        if not self._popcorn_base:
            raise ProviderError(
                "unreachable",
                "popcorn series host not configured (CS_UK_POPCORN_BASE_URL)",
            )
        return self._popcorn_base

    def torrent_hashes(self, external_id: str) -> dict[str, str]:
        """Quality→info-hash map recorded for ``external_id``, or {}.

        Compat view (#376 contract) over the candidate entries: first
        occurrence per quality wins. The POLICY sees all candidates —
        including same-quality repack variants with their seeds.
        """
        out: dict[str, str] = {}
        for cand in self._entries_for(external_id):
            out.setdefault(cand.quality, cand.info_hash)
        return out

    def _entries_for(self, external_id: str) -> list[TorrentCandidate]:
        """Recorded candidates for ``external_id``, touching recency."""
        entries = self._torrent_entries.get(external_id, [])
        if entries:
            self._torrent_entries.move_to_end(external_id)
        return entries

    def _require_engine(self) -> TorrentEngine:
        """The injected engine, else the lazy singleton; NONE of the two
        ⇒ LOUD typed verdict — never a silent pretend-stream."""
        engine = self._engine
        if engine is None:
            engine = get_engine()
        if engine is None:
            raise ProviderError("unreachable", "torrent engine not configured")
        return engine

    async def _get_payload(self, path: str, params: dict[str, str], http: httpx.AsyncClient) -> Any:
        return await self.get_json(f"{BASE_URL}{path}?{urlencode(params)}", http)

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        results = await self._search_movies(query, http)
        if self._popcorn_base:
            try:
                results += await self._search_series(query, http)
            except ProviderError as e:
                # The series host flaking must not sink the movies lane:
                # a series-side error logs and degrades to movies-only.
                log.warning("yts series search degraded: %s", e)
        return results

    async def _search_movies(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        payload = await self._get_payload(
            _LIST_PATH, {"query_term": query, "limit": str(_LIST_LIMIT)}, http
        )
        cards: list[SearchResult] = []
        for movie in _movies_of_list(payload):
            card = self._card(movie)
            if card is not None:
                self._record_torrents(card.id.removeprefix(f"{self.id}:"), movie)
                cards.append(card)
        return cards

    async def _search_series(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        """The Popcorn-API series page of the query (page 1 suffices for
        a search box; deep pagination is a browse concern)."""
        base = self._require_popcorn()
        url = f"{base}{POPCORN_SHOWS_PATH.format(page=1)}?sort=name&keywords={quote(query, safe='')}"
        payload = await self.get_json(url, http)
        cards: list[SearchResult] = []
        for show in _shows_of_page(payload):
            card = self._show_card(show)
            if card is not None:
                cards.append(card)
        return cards

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        if section == "movies":
            return await self._browse_movies(page, http)
        if section == "series":
            return await self._browse_series(page, http)
        raise ProviderError("not_found", f"unknown section: {section}")

    async def _browse_movies(
        self, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        payload = await self._get_payload(
            _LIST_PATH,
            {"sort_by": "date_added", "limit": str(_LIST_LIMIT), "page": str(page)},
            http,
        )
        data = _require_object(_require_object(payload, "payload").get("data"), "data")
        count = data.get("movie_count")
        limit = data.get("limit")
        if not isinstance(count, int) or not isinstance(limit, int):
            raise ProviderError("parse_failed", "listing pagination missing")
        results: list[SearchResult] = []
        for movie in _movies_of_list(payload):
            card = self._card(movie)
            if card is not None:
                self._record_torrents(card.id.removeprefix(f"{self.id}:"), movie)
                results.append(card)
        return results, count > page * limit

    async def _browse_series(
        self, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        """One Popcorn ``/shows/{page}`` page, newest-updated first.

        Has-next: a full page MIGHT have a next one — the upstream
        carries no movie_count analogue, so the listing itself is the
        truth (the same tolerant posture as a DLE pagination tail; one
        extra empty page at worst).
        """
        base = self._require_popcorn()
        url = f"{base}{POPCORN_SHOWS_PATH.format(page=page)}?sort=updated&order=-1"
        payload = await self.get_json(url, http)
        shows = _shows_of_page(payload)
        results: list[SearchResult] = []
        for show in shows:
            card = self._show_card(show)
            if card is not None:
                results.append(card)
        return results, len(results) >= _LIST_LIMIT

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        """Envelope duality on the bare IMDb id (module docstring):

        - series host configured → the #379 SERIES envelope (season 1),
        - unconfigured → the original movies-first envelope (#376).

        A group card's content lookup and PlaybackInfo both hit the
        bare id with no tail, so a YTS group MUST resolve here to
        something. Movies keep their playable id on the ``:__movie__``
        sentinel either way; the season rail's episode ids carry
        ``:sNeM`` tails — stream() tells the two apart by the tail
        grammar alone.
        """
        if not _IMDB_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        show = await self._show(external_id, http)
        if show is None:
            return await self._movie_content(external_id, http)
        return self._series_envelope(show, external_id, season_number=1)

    async def series_content(
        self, external_id: str, season_number: int, http: httpx.AsyncClient
    ) -> ContentResponse:
        """One season of a show as a v2 ContentResponse (ticket #379).

        Season 1 is the detail-page answer for a bare ``yts:<imdb>`` id
        (the client's "series = season 1" convention); the season rail
        asks for seasons 2+ by their number. A season the show's
        episodes[] never mentions answers the deterministic
        ``not_found`` — an empty season listing is NOT cached as an
        empty answer, it raises, so the envelope always carries a
        season when it exists at all.
        """
        if not _IMDB_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        if season_number < 1:
            raise ProviderError("not_found", "bad season number")
        show = await self._show(external_id, http)
        if show is None:
            # A season rail for an unconfigured lane: the LOUD typed
            # verdict (never a silent empty listing).
            self._require_popcorn()
            raise AssertionError("unreachable: _require_popcorn raises")
        return self._series_envelope(show, external_id, season_number=season_number)

    def _series_envelope(
        self, show: dict[str, Any], imdb: str, *, season_number: int
    ) -> ContentResponse:
        """Build the series ContentResponse for ONE season (#379).

        A season the show's episodes[] never mentions raises the
        deterministic ``not_found`` — an empty season listing is NOT
        cached as an empty answer, so the envelope always carries a
        season when the id was accepted at all.
        """
        title = _require_str(show.get("title"), "show title")
        seasons = self._seasons_of(show, title, want=season_number)
        if not seasons:
            raise ProviderError("not_found", f"no season {season_number} upstream")
        description = show.get("description")
        return ContentResponse(
            id=f"{self.id}:{imdb}",
            form="series",
            title=title,
            year=_show_year_of(show),
            description=description if isinstance(description, str) else "",
            poster=_show_poster_of(show),
            translations=[Translation(id="en", label="English")],
            seasons=seasons,
            translations_level="content",
            styles=frozenset(),
            genres=_genres_of(show),
            rating=_show_rating_of(show),
        )

    async def _movie_content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        """The original #376 movie envelope (moved verbatim so the
        unconfigured-series default stays byte-identical)."""
        payload = await self._get_payload(
            _DETAILS_PATH, {"imdb_id": external_id}, http
        )
        movie = _movie_of_details(payload)
        title = _display_title(movie)
        self._record_torrents(external_id, movie)
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            form="movie",
            title=title,
            year=_year_of(movie),
            description=_description_of(movie),
            poster=_poster_of(movie),
            # Single-audio original: ONE Translation entry, mirroring the
            # single-track pattern of the Ukrainian providers (their
            # `Translation(id="uk", label=…)` default) with the original
            # English track in its place — no dubs concept exists here.
            translations=[Translation(id="en", label="English")],
            seasons=[
                Season(
                    number=1,
                    episodes=[
                        Episode(
                            number=1,
                            id=f"{self.id}:{external_id}{MOVIE_SUFFIX}",
                            title=title,
                        )
                    ],
                )
            ],
            translations_level="content",
            styles=frozenset(),
            genres=_genres_of(movie),
            rating=_rating_of(movie),
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        """Magnet→engine handoff (spec #374 Stream contract): pick ONE
        torrent server-side, ensure an engine session for its magnet,
        return the engine's LAN URL as a post-remux progressive mp4 with
        empty headers (the facade's direct-302 posture stays intact).

        ``translation`` is deliberately ignored — English originals
        carry no dubs axis; the session IS the original audio.

        Episode ids (``yts:<imdb>:s<N>e<M>``) ride the SAME policy pick
        for their season (the show's per-episode quality maps collapse
        onto one torrent per season — the poptime mirror serves
        monolithic season packs), then the season number is the
        engine's ``file_hint`` seed: the adapter's deterministic file
        selection locates the right file inside the multi-file torrent
        (#379). The season discriminator (``@S<N>``) never reaches the
        engine — the hint is the SEASON, not the wire id.
        """
        del translation  # original audio only
        imdb, season = _parse_playable_id(content_id)
        if imdb is None or not _IMDB_RE.fullmatch(imdb):
            raise ProviderError("not_found", "bad external_id")
        engine = self._require_engine()
        if season is None:
            return await self._stream_movie(imdb, engine, http)
        return await self._stream_episode(imdb, season, engine, http)

    async def _stream_movie(
        self, imdb: str, engine: TorrentEngine, http: httpx.AsyncClient
    ) -> StreamResponse:
        """The #377 movie path: policy pick over the recorded movie
        candidates, magnet to the engine, no file hint."""
        entries = self._entries_for(imdb)
        if not entries:
            # Cold process: magnets are re-derivable from one details
            # call (yts.py module contract) — refresh, then re-read.
            await self.content(imdb, http)
            entries = self._entries_for(imdb)
        picked = select_torrent(entries)
        if picked is None:
            raise ProviderError("not_found", f"no torrents recorded for {imdb}")
        try:
            result = await engine.ensure_session(
                build_magnet(picked.info_hash), file_hint=None
            )
        except EngineUnavailable as e:
            raise ProviderError("unreachable", f"torrent engine unreachable: {e}") from e
        except EngineRejected as e:
            # Deterministic verdict on THIS torrent — never reads as a
            # dead lane (spec #374 error-surface note).
            raise ProviderError("not_found", "no seeders or dead torrent") from e
        return _torrent_stream_response(result)

    async def _stream_episode(
        self, imdb: str, season: int, engine: TorrentEngine, http: httpx.AsyncClient
    ) -> StreamResponse:
        """The #379 episode path: the season's single torrent, the
        season number as the file-selection hint."""
        show = await self._show(imdb, http)
        if show is None:
            self._require_popcorn()
            raise AssertionError("unreachable: _require_popcorn raises")
        torrents = self._season_torrents(show, season)
        picked = select_torrent(torrents)
        if picked is None:
            raise ProviderError(
                "not_found", f"no torrents recorded for {imdb} season {season}"
            )
        try:
            identifier = (
                picked.info_hash
                if picked.info_hash.startswith("magnet:")
                else build_magnet(picked.info_hash)
            )
            result = await engine.ensure_session(
                identifier, file_hint=_SEASON_HINT_FMT.format(season)
            )
        except EngineUnavailable as e:
            raise ProviderError("unreachable", f"torrent engine unreachable: {e}") from e
        except EngineRejected as e:
            raise ProviderError("not_found", "no seeders or dead torrent") from e
        return _torrent_stream_response(result)

    async def _show(self, imdb: str, http: httpx.AsyncClient) -> dict[str, Any] | None:
        """The Popcorn show object for ``imdb``, or ``None`` when the
        series host is not configured (the movies-first envelope falls
        out instead — typed verdicts are the SERIES-only paths' job).

        Cached for ``SHOW_CACHE_TTL_S``: one upstream fetch serves the
        whole PlaybackInfo → stream → VTT call chain (acceptance: the
        in-TTL chain must survive an upstream outage)."""
        base = self._popcorn_base
        if not base:
            return None
        now = time.monotonic()
        cached = self._show_cache.get(imdb)
        if cached is not None and now - cached[0] < SHOW_CACHE_TTL_S:
            self._show_cache.move_to_end(imdb)
            return cached[1]
        url = f"{base}{POPCORN_SHOW_PATH.format(imdb_id=imdb)}"
        payload = await self.get_json(url, http)
        show = _show_of_details(payload)
        self._show_cache[imdb] = (now, show)
        self._show_cache.move_to_end(imdb)
        while len(self._show_cache) > _SHOW_CACHE_MAX:
            self._show_cache.popitem(last=False)
        return show

    # -------------------------------------------------------------
    # Series card + season builders (#379)
    # -------------------------------------------------------------

    def _show_card(self, show: dict[str, Any]) -> SearchResult | None:
        """One Popcorn listing item → SearchResult; unidentifiable skip."""
        imdb = show.get("imdb_id")
        if not isinstance(imdb, str) or not _IMDB_RE.fullmatch(imdb):
            return None
        try:
            title = _require_str(show.get("title"), "show title")
        except ProviderError:
            return None
        return SearchResult(
            id=f"{self.id}:{imdb}",
            provider=self.id,
            form="series",
            styles=frozenset(),
            title=title,
            year=_show_year_of(show),
            poster=_show_poster_of(show),
            url=f"{self._popcorn_base}{POPCORN_SHOW_PATH.format(imdb_id=imdb)}",
            genres=_genres_of(show),
        )

    def _seasons_of(
        self, show: dict[str, Any], title: str, *, want: int
    ) -> list[Season]:
        """The wanted season's episodes as a v2 Season.

        Episode wire ids ride the canonical ``:sNeM`` tail grammar
        (``wire_identity.episode_wire_id``) so the resume reverse
        lookup, the season rail and the stream route all agree. An
        episode without torrents still lists — its play attempt is what
        surfaces the typed verdict.
        """
        episodes: list[Episode] = []
        for raw in _episodes_of_show(show):
            if raw.get("season") != want:
                continue
            number = raw.get("episode")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                continue
            episodes.append(
                Episode(
                    number=number,
                    id=episode_wire_id(self.id, self._imdb_of(show), want, number),
                    title=_episode_title(raw, title),
                    description=_episode_overview(raw),
                )
            )
        if not episodes:
            return []
        episodes.sort(key=lambda e: e.number)
        return [Season(number=want, episodes=episodes)]

    def _imdb_of(self, show: dict[str, Any]) -> str:
        imdb = show.get("imdb_id")
        return imdb if isinstance(imdb, str) else ""

    def _episode_torrents(self, episode: dict[str, Any]) -> list[TorrentCandidate]:
        """One episode's Popcorn ``torrents`` quality map → candidates.

        ``url`` magnets ride VERBATIM as the info_hash slot (the policy
        is blind to the difference; _stream_episode passes magnets
        through un-rebuilt) — seeds default 0 when absent.
        """
        torrents = episode.get("torrents")
        if not isinstance(torrents, dict):
            return []
        out: list[TorrentCandidate] = []
        for quality, entry in torrents.items():
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            seeds = entry.get("seeds")
            if not (isinstance(quality, str) and quality and isinstance(url, str) and url):
                continue
            out.append(
                TorrentCandidate(
                    quality=quality,
                    info_hash=url,
                    seeds=seeds if isinstance(seeds, int) and not isinstance(seeds, bool) else 0,
                )
            )
        return out

    def _season_torrents(self, show: dict[str, Any], season: int) -> list[TorrentCandidate]:
        """Every torrent candidate across the season's episodes.

        The poptime mirror serves monolithic season packs (the same
        magnet on every episode of the season), so the union across
        episodes IS the season's torrent table — the policy picks once
        for the season, the engine's file selection finds the episode
        file inside the pack.
        """
        merged: list[TorrentCandidate] = []
        for raw in _episodes_of_show(show):
            if raw.get("season") != season:
                continue
            merged.extend(self._episode_torrents(raw))
        return merged

    def _card(self, movie: dict[str, Any]) -> SearchResult | None:
        """One listing item → SearchResult; unidentifiable items skip."""
        imdb = movie.get("imdb_code")
        if not isinstance(imdb, str) or not _IMDB_RE.fullmatch(imdb):
            return None
        try:
            title = _display_title(movie)
        except ProviderError:
            return None
        return SearchResult(
            id=f"{self.id}:{imdb}",
            provider=self.id,
            form="movie",
            styles=frozenset(),
            title=title,
            year=_year_of(movie),
            poster=_poster_of(movie),
            url=f"{BASE_URL}{_DETAILS_PATH}?{urlencode({'imdb_id': imdb})}",
            genres=_genres_of(movie),
        )

    def _record_torrents(self, external_id: str, movie: dict[str, Any]) -> None:
        candidates = _torrent_candidates(movie)
        if not candidates:
            return
        self._torrent_entries[external_id] = candidates
        self._torrent_entries.move_to_end(external_id)
        while len(self._torrent_entries) > _TORRENT_ENTRIES_LIMIT:
            self._torrent_entries.popitem(last=False)


__all__ = ["YtsProvider"]
