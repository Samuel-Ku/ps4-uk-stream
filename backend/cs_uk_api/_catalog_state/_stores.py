"""Shared store layer of the catalog state package (spec #309 T5).

Every mutable store the internal modules (``resolution`` / ``warm`` /
``snapshot`` / ``search``) read or write lives HERE so those modules
form a dependency DAG with no cycle — this module is the leaf:

  - the TTL caches (home / search / content / blocklist / gated /
    sources / row-deep / deep-page) and the two cache-key constants
  - the warm content profiles (``_profiles``) with the install seam
  - the disk-backed resume store (playback positions + search-query
    history) and the user-state store (favorites / played / dub memory)
  - the snapshot store (persisted home snapshot, ticket #269)

The package hub (``__init__.py``) re-exports the public surface; the
internal modules import the store objects they need from here directly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import cast

from .. import config as _config
from ..cache import TtlCache
from ..models import HomeItem, HomeResponse, SearchGroup, SearchResult
from ..recommend import ItemProfile
from ..resume_store import ResumeStore
from ..snapshot_store import SnapshotStore
from ..user_state import UserStateStore
from ..wire_identity import provider_union

log = logging.getLogger("cs_uk_api.catalog_state.stores")

#: v3 (issue #70): the merged home view — «Новинки» + «Популярні зараз»
#: + the five type rows — is a curated snapshot, refreshed every 30 min.
home_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_home_s)

#: Multi-provider merged search (ticket #106): the native ``/api/search``
#: route and the Jellyfin facade share the SAME search cache (ADR-0003,
#: same 5m TTL as browse), so a query searched from either surface never
#: runs the provider fan-out twice. Key format and cache-key axes match
#: the route's contract exactly (``search:{provider}:{q}:{form}:{style}``).
search_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_search_s)

#: The native browse route's per-page cache (ADR-0003 browse TTL): the
#: one store ``main.py`` used to own outright (``_browse_cache``),
#: re-homed beside its siblings by the 2026-09-08 architecture review
#: (candidate 1) so no route module owns a store any more.
browse_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_search_s)

#: Content-detail + blocked-country caches (ADR-0003). Moved here from
#: ``main.py`` so the Jellyfin facade's ticket #105 detail resolver reads
#: the SAME stores the native ``/api/content`` route uses — one TTL, one
#: cache key shape (``content:{provider}:{external}``), one clear().
content_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_content_s)
blocklist_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_content_s)

#: Deep-row extension caches (spec #305): the merged pool BEYOND a
#: home row's snapshot (``row_deep_cache``, keyed per row kind) and the
#: underlying provider browse pages 2..N (``deep_page_cache``, keyed per
#: provider/section/page). Both use the browse-cache TTL so repeated
#: scroll passes are instant and upstream load stays bounded — the same
#: TtlCache machinery as the per-page browse cache, no new layer.
#: Cleared with the home snapshot on rebuild (the pools are
#: snapshot-anchored).
row_deep_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_search_s)
deep_page_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_search_s)

#: Subscription-gate verdict store: ``content:{provider}:{external}`` →
#: True (gated) / False (known-good). Written by the catalog sweep and
#: read by the routes so a gated verdict survives across home rebuilds
#: without re-resolving (TTL is deliberately longer than the home cache,
#: see ``Settings.cache_gated_s``).
gated_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_gated_s)

#: v3 (issue #60): side cache keyed by group_key → {provider →
#: SearchResult}. Populated from the raw SearchResult listings the home
#: fan-out collected. Same TTL as the home cache (ADR-0003), cleared by
#: the same restart.
#
#: v3 (ticket #101): this is ALSO the Jellyfin facade's resolution map —
#: ``/Items/{g2:...}`` resolves provider+external from it. ``g2:`` ids
#: are deliberately NOT self-resolving; a cold cache yields 404
#: ("item unavailable"), which Jellyfin clients tolerate.
sources_cache: TtlCache = TtlCache(default_ttl_s=_config.SETTINGS.cache_home_s)

_HOME_KEY = "home:v1"
_SOURCES_KEY = "home:sources:v1"


# ------------------------------------------------- catalog snapshot (ADR-0010)
#
# The catalog's derived state — the group index beside sources_cache, and
# the search registrations that must survive a snapshot replacement — is
# owned by ONE value, ``CatalogState``. ``apply_snapshot`` is the single
# writer of the map + index pair (the persisted cold start and a finished
# rebuild both go through it), so the two cannot diverge; the two
# sanctioned clears in the warm path go through ``invalidate``.
#
# Why it exists: issue #420. A group key returned by search answered
# ``/Items/{id}`` with 404 seconds after it had resolved, because a
# snapshot replacement replaced the map and index WHOLE and silently
# dropped the registration. Measured on the deployment: 16 keys
# registered, the next replacement dropped exactly 16, the read 404-ed.
# ADR-0010 records the decision.


@dataclass(frozen=True)
class GroupIndexEntry:
    """One indexed group key: the home row's item + its row kind.

    ``home_item`` is None for a key that only a SEARCH registered — the
    registered layer of the catalog state (ADR-0010). Routes never read
    this shape: they read ``GroupResolution`` below, so the two layers
    cannot be mixed by accident.
    """

    home_item: HomeItem | None
    row_type: str | None


class GroupOrigin(str, Enum):
    """Which layer of the catalog state carries a group key (ADR-0010)."""

    #: A home-snapshot row carries it — the card and row kind are real.
    SNAPSHOT = "snapshot"
    #: Only a search registered it. It has sources but no card, and it
    #: lives on the search TTL that created it, not the snapshot cycle.
    REGISTERED = "registered"


@dataclass(frozen=True)
class SnapshotEntry:
    """One distinct home-snapshot card with the row kind that surfaced it.

    The snapshot layer's iteration unit: the row kind rides along, so a
    caller (counts, person filmography, the similar shelf, the genre
    rails) never looks it up a second time.
    """

    card: HomeItem
    row_type: str | None


@dataclass(frozen=True)
class GroupResolution:
    """THE typed answer for one group key — the whole read side, once.

    Before this value the read side asked the index for a card, then the
    resolution map for sources, then the map again for a year or a genre
    list, and every caller had to know that order. One lookup now answers
    all of it, and ``origin`` says which layer answered.

    ``card`` and ``row_type`` are the SNAPSHOT layer's (ADR-0010: the
    snapshot wins a key both layers hold). ``sources`` is the resolution
    map's ordered card list — first-seen provider order, the same order
    the home row's chip strip shows — which is the union of the layers,
    because a registration is merged into the map at registration time.
    ``year``/``genres`` keep their card-then-sources fallback.
    """

    group_key: str
    origin: GroupOrigin
    card: HomeItem | None
    row_type: str | None
    sources: tuple[SearchResult, ...]
    year: int | None
    genres: tuple[str, ...]

    @property
    def providers(self) -> tuple[str, ...]:
        """Provider ids in first-seen order (the chip strip).

        The snapshot path echoes the CARD's own provider list, not the
        map's provider keys — the wire contract ``GroupContentResponse``
        has always had, preserved deliberately for a shared key where a
        registration added providers the snapshot row does not list.
        """
        if self.card is not None:
            return tuple(self.card.providers)
        return tuple(s.provider for s in self.sources)

    def source(self, provider: str) -> SearchResult | None:
        """One provider's card in this group, or None (source-switch routes)."""
        for s in self.sources:
            if s.provider == provider:
                return s
        return None


def _first_year(card: HomeItem | None, sources: tuple[SearchResult, ...]) -> int | None:
    """The card's year, else the first source carrying one (ticket #233)."""
    if card is not None and card.year is not None:
        return card.year
    for s in sources:
        if s.year is not None:
            return s.year
    return None


def _first_genres(
    card: HomeItem | None, sources: tuple[SearchResult, ...]
) -> tuple[str, ...]:
    """The card's genres, else the first source carrying any (ticket #233)."""
    if card is not None and card.genres:
        return tuple(card.genres)
    for s in sources:
        if s.genres:
            return tuple(s.genres)
    return ()


class CatalogState:
    """The catalog snapshot's owned derived state (ADR-0010).

    Owns the two pieces that must move together: the group index beside
    ``sources_cache`` and the live search registrations. The snapshot
    rows and the resolution map themselves stay in the shared TTL caches
    above (ADR-0003 owns their TTLs) — this object owns *who writes
    them* and *what survives a replacement*.

    ``now`` is the injectable clock (the ``ResumeStore`` idiom) so tests
    drive registration expiry deterministically instead of sleeping.
    """

    def __init__(
        self,
        *,
        search_ttl_s: int | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._index: dict[str, GroupIndexEntry] = {}
        #: group key -> (expires_at, the providers AND cards the SEARCH
        #: layer owns for it). Deliberately NOT the resolution map's whole
        #: entry: a snapshot provider — its card included — is the
        #: snapshot's to replace, so a registration carries only what a
        #: search contributed. They outlive a snapshot replacement and
        #: expire on the search TTL that created them, never on the
        #: snapshot's cycle.
        self._registrations: dict[str, tuple[float, dict[str, SearchResult]]] = {}
        self._search_ttl_s = (
            _config.SETTINGS.cache_search_s if search_ttl_s is None else search_ttl_s
        )
        self._now = now

    # ----------------------------------------------------------------- reads

    def entry(self, group_key: str) -> GroupIndexEntry | None:
        """Indexed entry for a group key, or None.

        The owner's own introspection seam (a test asserting what the
        index carries), NOT a read accessor: routes answer through
        ``group_resolution`` / ``snapshot_entries``.
        """
        return self._index.get(group_key)

    def group_resolution(self, group_key: str) -> GroupResolution | None:
        """One typed answer for a group key, or None when no layer holds it.

        Reads both layers ONCE — the index entry and the resolution map —
        so no caller has to know that order, and reports which layer
        answered (``origin``). A key that only a search registered has no
        card and no row kind, but does have sources; a key both layers
        hold answers from the snapshot and carries the merged sources.
        """
        entry = self._index.get(group_key)
        per_provider = self.sources().get(group_key)
        if entry is None and per_provider is None:
            return None
        card = entry.home_item if entry is not None else None
        sources = tuple(per_provider.values()) if per_provider else ()
        return GroupResolution(
            group_key=group_key,
            origin=GroupOrigin.SNAPSHOT if card is not None else GroupOrigin.REGISTERED,
            card=card,
            row_type=entry.row_type if entry is not None else None,
            sources=sources,
            year=_first_year(card, sources),
            genres=_first_genres(card, sources),
        )

    def sources(self) -> dict[str, dict[str, SearchResult]]:
        """The current resolution map (``group_key -> {provider: card}``).

        Read THROUGH the owner (ADR-0010): the map's cache key and its
        empty-vs-absent handling stay in this layer, so resolution reads
        and writes it through one interface instead of reaching for the
        cache's key itself.
        """
        return cast(
            dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
        )

    def snapshot_entries(self) -> tuple[SnapshotEntry, ...]:
        """The SNAPSHOT layer's distinct cards, in row-then-item order.

        The iteration surface (ADR-0010): counts, person filmography, the
        similar shelf and the genre rails walk snapshot content only, and
        this returns exactly that layer. A key a search registered has no
        card and cannot appear here, so a route cannot mix the layers by
        forgetting to filter — the pre-ADR-0010 index handed out entries
        with ``home_item=None`` and every site re-checked for None.
        """
        seen: set[str] = set()
        out: list[SnapshotEntry] = []
        for ent in self._index.values():
            card = ent.home_item
            if card is None or card.group_key in seen:
                continue
            seen.add(card.group_key)
            out.append(SnapshotEntry(card=card, row_type=ent.row_type))
        return tuple(out)

    # -------------------------------------------------- the one apply step

    def apply_snapshot(
        self,
        home: HomeResponse,
        sources: Mapping[str, dict[str, SearchResult]] | None,
        *,
        persist: bool = False,
    ) -> None:
        """Install a snapshot: rows, resolution map, index. THE apply step.

        The only writer of the map + index pair, called by both sites that
        replace the derived state (the persisted cold start and a finished
        rebuild). Live search registrations are merged into the NEW map
        and index before either is installed — snapshot content wins
        first-seen — so a rebuild can no longer drop a key a client just
        opened (ADR-0010). The deep-row pools are anchored to the previous
        snapshot's page-1 items and are always dropped.

        ``sources`` is None for a persisted snapshot written before the
        resolution map was persisted: the rows and the index are installed
        and the map is left alone (the pre-#269 cold-start shape).
        ``persist`` writes the versioned snapshot file from the SNAPSHOT's
        own map, never the merged one — a rebuild only, since the cold
        start is reading it, and a registration is search-TTL state that
        must not outlive its promise on disk.
        """
        merged: dict[str, dict[str, SearchResult]] | None = None
        if sources is not None:
            merged = {key: dict(per_provider) for key, per_provider in sources.items()}
        entries = self._entries_from_rows(home)
        for key, per_provider in self._live_registrations().items():
            if merged is not None:
                target = merged.setdefault(key, {})
                for pid, card in per_provider.items():
                    target.setdefault(pid, card)
            entries.setdefault(key, GroupIndexEntry(home_item=None, row_type=None))
        home_cache.set(_HOME_KEY, home)
        if merged is not None:
            sources_cache.set(_SOURCES_KEY, merged)
        self._replace_index(entries)
        row_deep_cache.clear()
        if persist and sources is not None:
            # Persist the SNAPSHOT's map, never the merged one (2026-09-12
            # audit, finding 2): the file is the next cold start's
            # snapshot, while a search registration is search-TTL state —
            # writing it would resurrect an expired promise after a
            # restart and grow the file with a session's searches. The
            # None branch means a legacy snapshot with no map at all:
            # write nothing rather than an empty map over a good file.
            _snapshot_store().save(home, dict(sources))

    # --------------------------------------------- search registration (#106)

    def register_search(self, groups: Sequence[SearchGroup]) -> None:
        """Fold search-group unions into the map + index, and remember them.

        Ticket #106: the facade's search must open in the #105 detail
        surface, and only keys the resolution map carries resolve. A search
        covers the whole catalog — most results are NOT in the home
        snapshot — so each merged group's provider union is registered
        under every member key, first-seen provider order preserved and
        providers the map already knows left untouched.

        Each key is ALSO recorded here with the search TTL (ADR-0010).
        That record is what ``apply_snapshot`` merges into a replacement,
        so the promise a search makes — your results stay actionable for
        as long as the results themselves are cached — survives a rebuild.
        Re-registering a key extends its TTL, never shortens it.

        The record is the **search layer's own union** for the key — the
        providers it returned AND their cards, plus those of a still-live
        earlier registration — never the resolution map's whole entry.
        Recording the whole entry (the pre-2026-09-13 shape) made the
        search layer a storer of snapshot state: a provider the next
        snapshot dropped rode the registration back as a trailing chip for
        the rest of the TTL, and the snapshot's card could outlive the
        snapshot that chose it whenever a search also returned that
        provider. A snapshot provider is the snapshot's to replace, card
        included; a search must not resurrect either (audit finding 4 of
        the 2026-09-12 review).
        """
        if not groups:
            return
        existing = self.sources()
        now = self._now()
        expires_at = now + self._search_ttl_s
        changed = False
        for group in groups:
            union = provider_union(group.sources)
            for key in group.member_keys or [group.group_key]:
                current = existing.get(key)
                if current is None:
                    existing[key] = dict(union)
                    changed = True
                else:
                    merged = dict(current)
                    for pid, card in union.items():
                        merged.setdefault(pid, card)
                    if merged != current:
                        existing[key] = merged
                        changed = True
                if key not in self._index:
                    self._index[key] = GroupIndexEntry(home_item=None, row_type=None)
                    changed = True
                # First-seen wins, and only search-owned entries are ever
                # recorded, so no snapshot card can ride forward.
                owned: dict[str, SearchResult] = {}
                earlier = self._registrations.get(key)
                if earlier is not None and earlier[0] >= now:
                    owned.update(earlier[1])
                for pid, card in union.items():
                    owned.setdefault(pid, card)
                self._registrations[key] = (expires_at, owned)
        if changed:
            # Re-set refreshes the whole map's TTL (ADR-0003): a search
            # extends the snapshot's life, never shortens it.
            sources_cache.set(_SOURCES_KEY, existing)

    # ------------------------------------------------------ sanctioned clear

    def invalidate(self, *, reason: str) -> None:
        """Drop the cached snapshot so the next read rebuilds it.

        A SANCTIONED event-driven invalidation (ADR-0010): the rows are
        derived from the active taste profile, so a warm profile landing (or
        an LLM refresh) makes the cached snapshot wrong — a correctness bug,
        not a TTL decision. The deep-row pools are snapshot-anchored, so
        they go with it. Search registrations are deliberately KEPT: they
        are the client's promise rather than derived state, and
        ``apply_snapshot`` re-merges them into whatever replaces this.
        """
        log.info("catalog snapshot invalidated reason=%s", reason)
        home_cache.clear()
        row_deep_cache.clear()

    # -------------------------------------------------------- test seams (#330)
    #
    # The suite's two handles on the owned derived state. They exist so a
    # test never reaches for ``sources_cache`` (or its cache key) again:
    # the map, the index and the registrations are ONE owned value, and a
    # test that poked the map alone left the index describing a catalog
    # the map no longer had — the exact divergence ADR-0010 prevents.

    def seed_sources(
        self, mapping: Mapping[str, Mapping[str, SearchResult]]
    ) -> None:
        """Install a pre-built resolution map (test seed).

        The ``{group_key: {provider: card}}`` shape a home build or a
        search registration produces, handed over whole so a test can
        exercise resolution without a provider fan-out or a home build.

        Writes the map ONLY: a seeded key is neither a snapshot card nor
        a registration, so a read through ``group_resolution`` sees it
        with the REGISTERED origin, no row kind, and no index entry —
        which is what a bare ``sources_cache`` write did before this
        seam existed.
        """
        sources_cache.set(
            _SOURCES_KEY,
            {key: dict(per_provider) for key, per_provider in mapping.items()},
        )

    def reset(self) -> None:
        """Drop the owned derived state: map, index, registrations.

        The reset seam, and the honest meaning of "cold": the three
        pieces move together, so a test cannot empty the map and leave
        the index behind. Called by conftest before every test (beside a
        fresh owner) instead of clearing ``sources_cache`` in a loop of
        unrelated stores.
        """
        sources_cache.clear()
        self._index.clear()
        self._registrations.clear()

    # ------------------------------------------------------------ internals

    def _live_registrations(self) -> dict[str, dict[str, SearchResult]]:
        """Registrations still inside their search TTL (expired ones drop)."""
        now = self._now()
        live: dict[str, dict[str, SearchResult]] = {}
        for key, (expires_at, per_provider) in list(self._registrations.items()):
            if expires_at < now:
                del self._registrations[key]
                continue
            live[key] = per_provider
        return live

    @staticmethod
    def _entries_from_rows(home: HomeResponse) -> dict[str, GroupIndexEntry]:
        """Fresh index entries from a snapshot's rows (row then item order)."""
        entries: dict[str, GroupIndexEntry] = {}
        for row in home.rows:
            for item in row.items:
                for key in item.member_keys or [item.group_key]:
                    if key not in entries:
                        entries[key] = GroupIndexEntry(home_item=item, row_type=row.type)
        return entries

    def _replace_index(self, entries: dict[str, GroupIndexEntry]) -> None:
        """Replace the index in place, logging the registrations that lapse.

        In place, so an already-handed-out ``entries()`` view stays live
        (the pre-ADR-0010 ``_set_group_index`` semantics). The log line is
        the fix's observable: before ADR-0010 it fired on every
        replacement; it now fires only for a registration that has aged
        out at the search TTL, so a carried-forward key stays silent
        (issue #420's instrument, kept as the residual check).
        """
        retired = [
            key
            for key, ent in self._index.items()
            if ent.home_item is None and key not in entries
        ]
        if retired:
            log.info(
                "group index replaced: %d registered key(s) retired"
                " (search TTL, not the snapshot cycle)",
                len(retired),
            )
        self._index.clear()
        self._index.update(entries)


#: The process-wide catalog snapshot owner (ADR-0010).
_CATALOG = CatalogState()


def catalog_state() -> CatalogState:
    """The process-wide catalog snapshot owner (ADR-0010)."""
    return _CATALOG


def install_catalog_state(state: CatalogState) -> None:
    """Swap the process-wide catalog state (test seam, spec #309 T5).

    Tests CONSTRUCT an isolated catalog through this instead of reaching
    for a private index clear — the escape hatch the 2026-09-12 review
    (candidate 3) removed.
    """
    global _CATALOG
    _CATALOG = state


def reset_catalog_state() -> None:
    """Drop the owned derived state: the map, the index, the registrations.

    The suite's reset seam. Prefer this to clearing ``sources_cache``:
    the three pieces are one owned value, and emptying only the map is
    what let the layers disagree before ADR-0010 (issue #420).
    """
    _CATALOG.reset()


def seed_group_sources(
    mapping: Mapping[str, Mapping[str, SearchResult]],
) -> None:
    """Install a pre-built resolution map (the suite's seed seam).

    The one way a test hands the owner group sources it did not build —
    the same shape ``register_search_groups`` writes, without the search
    bookkeeping. See ``CatalogState.seed_sources`` for what it does and
    does not install.
    """
    _CATALOG.seed_sources(mapping)


def get_group_entry(group_key: str) -> GroupIndexEntry | None:
    """Indexed entry for a group key, or None."""
    return _CATALOG.entry(group_key)


def group_resolution(group_key: str) -> GroupResolution | None:
    """THE process-wide group lookup (ADR-0010): one typed answer, or None."""
    return _CATALOG.group_resolution(group_key)


def snapshot_entries() -> tuple[SnapshotEntry, ...]:
    """The process-wide snapshot layer's entries, for iteration."""
    return _CATALOG.snapshot_entries()


# ---------------------------------------------------------- profiles (#252)

#: Content-page taste profiles of the home-snapshot groups (spec #252),
#: keyed by ``g2:`` group key. Built in the background by
#: ``warm._warm_profiles``; in-memory only — a restart re-warms
#: (bounded by the content cache).
_profiles: dict[str, ItemProfile] = {}


def get_profiles() -> Mapping[str, ItemProfile]:
    """Read-only view of the warm content profiles (spec #252).

    The Similar shelf (spec #267 T1) scores home candidates against
    these; a cold store (empty) falls back to the genre-matching shelf.
    """
    return _profiles


def install_profiles(mapping: Mapping[str, ItemProfile]) -> None:
    """Replace the warm content profiles wholesale (spec #309 T4).

    The install seam for the profile store: the interface exposes it so
    tests seed state through the same route-visible surface instead of
    mutating ``_profiles`` directly (step 6 turns this into the store's
    own install/get/warm contract).
    """
    _profiles.clear()
    _profiles.update(mapping)


# ------------------------------------------------- playback (#214/#248)

#: Per-item playback positions reported by the client (ticket #214, then
#: persisted per spec #247 / ticket #248). The facade has a single fixed
#: user (D4), so no per-user dimension. The store is disk-backed — a
#: versioned JSON file next to the poster disk cache — and survives
#: restarts (ADR-0003 note, spec #247). ``SETTINGS.resume_path`` is None
#: in the test suite (conftest), keeping the pre-#248 memory-only
#: semantics there.
_resume_store: ResumeStore = ResumeStore(_config.SETTINGS.resume_path)


def _store() -> ResumeStore:
    return _resume_store


def install_resume_store(store: ResumeStore) -> None:
    """Swap the process-wide resume store (test seam, spec #309 T5).

    Tests inject a store over a temp path or with a fake clock through
    this instead of rebinding the module attribute — the seam survives
    the internal-module split (the store itself lives in ``_stores``).
    """
    global _resume_store
    _resume_store = store


def record_playback(
    item_id: str,
    position_ticks: int,
    *,
    runtime_ticks: int | None = None,
    flush: bool = False,
) -> None:
    """Record the client's playback position (``Sessions/Playing/*``).

    Last report wins — the client streams Progress heartbeats while
    playing and a final Stopped report, so the newest position is the
    most accurate. Zero/negative positions (a just-started item) are
    ignored; a later positive report overwrites. ``flush=True`` (the
    Stopped path, ticket #248) writes the state file synchronously;
    heartbeat reports are debounced by the store.
    """
    _store().record(item_id, position_ticks, runtime_ticks=runtime_ticks, flush=flush)


def playback_entries() -> dict[str, tuple[int, int | None]]:
    """item_id -> (position_ticks, runtime_ticks|None), most-progressed
    first (ticket #214). NextUp reads this (spec #247 keeps its
    most-progressed-per-series semantics unchanged); the runtime rides
    along so the DTO carries RunTimeTicks (#250).
    """
    return _store().positions_entries()


def recent_playback_entries(limit: int = 20) -> dict[str, tuple[int, int | None]]:
    """item_id -> (position_ticks, runtime_ticks|None), most recently
    updated first, capped at ``limit`` — the resume row (ticket #249),
    with the runtime for the wire bar (#250).
    """
    return _store().recent_entries(limit)


def recent_history_entries(limit: int = 20) -> list[str]:
    """item_ids in most-recently-seen order, active AND finished
    (spec #272 «Нещодавно переглянуто»)."""
    return _store().history(limit)


def clear_playback() -> None:
    """Drop all recorded positions (test isolation, #214)."""
    _store().clear()


def flush_playback() -> None:
    """Flush pending playback state to disk (lifespan shutdown, #248)."""
    _store().flush()


# ---------------------------------------------------------- user state (#257)

#: Favorites + played marks (spec #257): separate versioned store so the
#: two specs' version bumps never collide. ``SETTINGS.user_state_path``
#: is None in the test suite (conftest), keeping the memory-only
#: semantics there.
_user_state_store: UserStateStore = UserStateStore(_config.SETTINGS.user_state_path)


def user_state_store() -> UserStateStore:
    return _user_state_store


def set_favorite(item_id: str, is_favorite: bool) -> None:
    """Mark or unmark an item as favorite (spec #257)."""
    _user_state_store.set_favorite(item_id, is_favorite)


def set_played(item_id: str, played: bool) -> None:
    """Mark or unmark an item as played (spec #257)."""
    _user_state_store.set_played(item_id, played)


def is_favorite(item_id: str) -> bool:
    return _user_state_store.is_favorite(item_id)


def is_played(item_id: str) -> bool:
    return _user_state_store.is_played(item_id)


def remember_dub(series_group_key: str, translation_label: str) -> None:
    """Record the viewer's dub choice for a series (spec #276)."""
    _user_state_store.remember_dub(series_group_key, translation_label)


def dub_for(series_group_key: str) -> str | None:
    """The remembered dub label for a series (spec #276), or None."""
    return _user_state_store.dub_for(series_group_key)


def dub_memory() -> dict[str, str]:
    """The whole dub memory (group key → label), for tests (spec #276)."""
    return _user_state_store.dub_memory()


def clear_user_state() -> None:
    """Drop all favorites/played marks (test isolation, #257)."""
    _user_state_store.clear()


def record_search_query(query: str) -> None:
    """Record a search query as taste signal (spec #252)."""
    _store().record_query(query)


def recent_search_queries() -> list[str]:
    """Search queries, newest first (spec #252)."""
    return _store().recent_queries()


# ---------------------------------------------------- snapshot store (#269)

#: The persisted home snapshot + group-key resolution map (ticket #269):
#: a cold process start serves the stale snapshot instantly at ANY age
#: while the full fan-out rebuild runs in the background. Memory-only in
#: the test suite (``SETTINGS.snapshot_path`` is None in conftest).
_snapshot_store_ref: SnapshotStore = SnapshotStore(_config.SETTINGS.snapshot_path)


def _snapshot_store() -> SnapshotStore:
    """The module-level snapshot store (memory-only in the test suite)."""
    return _snapshot_store_ref


def install_snapshot_store(store: SnapshotStore) -> None:
    """Swap the process-wide snapshot store (test seam, spec #309 T5).

    Tests restore the previous store after a temp-path exercise instead
    of leaking a store that keeps serving the temp file.
    """
    global _snapshot_store_ref
    _snapshot_store_ref = store


def clear_snapshot_store() -> None:
    """Re-instantiate the store from the current ``SETTINGS.snapshot_path``
    (tests that flip the path knob)."""
    global _snapshot_store_ref
    _snapshot_store_ref = SnapshotStore(_config.SETTINGS.snapshot_path)
