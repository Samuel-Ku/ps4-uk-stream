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

from collections.abc import Mapping

from .. import config as _config
from ..cache import TtlCache
from ..recommend import ItemProfile
from ..resume_store import ResumeStore
from ..snapshot_store import SnapshotStore
from ..user_state import UserStateStore

#: v3 (issue #70): the merged home view — «Новинки» + «Популярні зараз»
#: + the five type rows — is a curated snapshot, refreshed every 30 min.
home_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_home_s)

#: Multi-provider merged search (ticket #106): the native ``/api/search``
#: route and the Jellyfin facade share the SAME search cache (ADR-0003,
#: same 5m TTL as browse), so a query searched from either surface never
#: runs the provider fan-out twice. Key format and cache-key axes match
#: the route's contract exactly (``search:{provider}:{q}:{form}:{style}``).
search_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_search_s)

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
