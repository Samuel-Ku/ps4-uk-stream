"""YTS movies provider — the original-English catalog lane (spec #374,
ticket #376).

Movies ONLY this pass: research #366 found every Popcorn SERIES host
dead (no stable public clearnet host; URLs ride a signed DHT record),
so series acceptance criteria are deferred — recorded on ticket #376,
not faked here.

The upstream is the YTS v2 JSON API (`https://yts.gg/api/v2/`,
verified live 2026-08-25; response banner announces the base moving to
``https://movies-api.accel.li/api/v2/``). Both hosts are declared in
``allowed_hosts`` so ``safe_get`` admits a redirect hop during the
in-flight migration; no failover loop beyond that — minimum surface.

Wire identity: the external id IS the IMDb code (``tt1160419``) —
stable across upstream listing churn, which resume/user-state depend
on (spec #374 decision). Display title is ``title_english`` (the
original English); YTS ``language`` is display metadata only, items
are mapped as listed.

Torrent payloads: ``torrents[]`` arrives embedded in both list and
details responses. The parsed candidates are threaded onto the provider
instance (LRU-bounded; :meth:`YtsProvider.torrent_hashes` is the
compat quality→hash view) so playback (#377) can build magnets without
a second upstream call — magnets re-derivable at stream-time from
``movie_details.json?imdb_id=…`` either way.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

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
from ..torrent_engine import (
    EngineRejected,
    EngineUnavailable,
    TorrentEngine,
    get_engine,
)
from ..wire_identity import MOVIE_SUFFIX
from .base import BaseProvider, ProviderError

BASE_URL = "https://yts.gg"
#: Migration base announced by the API banner (research #366 §5) —
#: declared so an allowlist-followed redirect keeps working.
MIRROR_BASE_URL = "https://movies-api.accel.li"

_LIST_PATH = "/api/v2/list_movies.json"
_DETAILS_PATH = "/api/v2/movie_details.json"

_SECTIONS = (Section(id="movies", title="Фільми", form="movie"),)

#: Boundary validation: only a well-formed IMDb code may reach the URL.
_IMDB_RE = re.compile(r"tt\d{7,8}")

_LIST_LIMIT = 50

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


#: LRU bound on recorded torrent candidates (review finding, #377):
#: search/browse/content thread entries for every parsed item, so an
#: unbounded per-instance map would grow with the catalog over process
#: lifetime. OrderedDict move-to-end-on-access; oldest id evicted.
_TORRENT_ENTRIES_LIMIT = 512


class YtsProvider(BaseProvider):
    id = "yts"
    name = "YTS"
    types = ("movie",)
    sections = _SECTIONS
    #: Home composition: newest = page-N ``sort_by=date_added`` listing
    #: (spec #263 recent rows read this once registered).
    newest_section = "movies"
    #: The YTS API base + its announced migration mirror — every fetch
    #: AND redirect hop is checked against this declaration (ADR-0005).
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

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        if section != "movies":
            raise ProviderError("not_found", f"unknown section: {section}")
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

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        if not _IMDB_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
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
        """
        del translation  # original audio only
        imdb, _, tail = content_id.partition(":")
        if not _IMDB_RE.fullmatch(imdb) or tail not in ("", MOVIE_SUFFIX[1:]):
            raise ProviderError("not_found", "bad external_id")
        engine = self._require_engine()
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
        return StreamResponse(url=result.url, type="mp4", headers={})

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
