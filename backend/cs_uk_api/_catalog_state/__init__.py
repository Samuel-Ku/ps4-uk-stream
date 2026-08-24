"""Shared catalog state: home snapshot + group-key resolution map (ticket #101).

Both the native ``/api/*`` routes and the Jellyfin facade read the same
in-memory home snapshot (``HomeResponse``) and the same
``group_key -> {provider: SearchResult}`` resolution map, so a card that
surfaced in ``/api/home`` resolves identically from ``/Items/{id}`` with
no second upstream fetch.

ADR-0003 (cache contract) holds here: TTL-only, in-memory, no persisted
domain schema, no version token. ``load_home()`` is the single builder —
a cache hit on a facade request does the same round trips the native
``/api/home`` route would do, because it IS the same code path.

Internal split (spec #309 T5): the implementation lives in the internal
modules ``_stores`` / ``resolution`` / ``warm`` / ``snapshot`` /
``search`` behind this hub. Every name the interface (``catalog``), the
routes and the tests imported from this module is re-exported here
unchanged — the split is interface-neutral.
"""

from __future__ import annotations

from ..providers import PROVIDERS  # noqa: F401  (catalog_state.PROVIDERS is the registry)
from . import resolution, search, snapshot, warm  # noqa: F401
from ._stores import (
    _HOME_KEY,
    _SOURCES_KEY,
    _resume_store,
    _snapshot_store,
    GroupIndexEntry,
    _clear_group_index,
    _merge_search_keys,
    _set_group_index,
    all_home_cards_in_index_order,
    blocklist_cache,
    clear_playback,
    clear_snapshot_store,
    clear_user_state,
    content_cache,
    deep_page_cache,
    dub_for,
    dub_memory,
    flush_playback,
    gated_cache,
    get_group_entry,
    get_profiles,
    group_index_entries,
    home_cache,
    install_profiles,
    install_resume_store,
    install_snapshot_store,
    is_favorite,
    is_played,
    playback_entries,
    recent_history_entries,
    recent_playback_entries,
    recent_search_queries,
    record_playback,
    record_search_query,
    remember_dub,
    row_deep_cache,
    search_cache,
    set_favorite,
    set_played,
    sources_cache,
    user_state_store,
)
from .resolution import (
    _GATE_CHECK_CONCURRENCY,
    CONTENT_RETRY_DELAY_S,
    GATE_CHECK_TIMEOUT_S,
    MAX_TRANSLATION_SOURCES,
    WARM_WAIT_S,
    PlaybackEpisodePairing,
    await_uakino_ready,
    cached_provider_content,
    episode_group_key,
    filter_gated_items,
    group_key_for_external,
    is_hard_unavailable,
    ordered_translation_candidates,
    peek_group_content,
    playback_episode_pair,
    playback_translations,
    record_dub_choice,
    register_search_groups,
    resolve_group,
    resolve_group_content,
    should_skip_uakino_in_fanout,
)
from .search import merged_search
from .snapshot import extend_row_pool, get_home, load_home
from .warm import recommendation_stats, refresh_profile

__all__ = [
    "CONTENT_RETRY_DELAY_S",
    "GATE_CHECK_TIMEOUT_S",
    "GroupIndexEntry",
    "MAX_TRANSLATION_SOURCES",
    "WARM_WAIT_S",
    "_GATE_CHECK_CONCURRENCY",
    "_clear_group_index",
    "_merge_search_keys",
    "_set_group_index",
    "all_home_cards_in_index_order",
    # stores / caches
    "_HOME_KEY",
    "_SOURCES_KEY",
    "PlaybackEpisodePairing",
    "_resume_store",
    "_snapshot_store",
    "get_group_entry",
    "group_index_entries",
    "await_uakino_ready",
    "blocklist_cache",
    "cached_provider_content",
    "clear_playback",
    "clear_snapshot_store",
    "clear_user_state",
    "content_cache",
    "deep_page_cache",
    "dub_for",
    "dub_memory",
    "episode_group_key",
    # search / snapshot / warm
    "extend_row_pool",
    "filter_gated_items",
    "flush_playback",
    "gated_cache",
    "get_home",
    "get_profiles",
    "group_key_for_external",
    "home_cache",
    "install_profiles",
    "install_resume_store",
    "install_snapshot_store",
    "is_favorite",
    "is_hard_unavailable",
    "is_played",
    "load_home",
    "merged_search",
    "ordered_translation_candidates",
    "peek_group_content",
    "playback_entries",
    "playback_episode_pair",
    "playback_translations",
    "recent_history_entries",
    "recent_playback_entries",
    "recent_search_queries",
    "recommendation_stats",
    "record_dub_choice",
    "record_playback",
    "record_search_query",
    "refresh_profile",
    "register_search_groups",
    "remember_dub",
    "resolve_group",
    "resolve_group_content",
    "row_deep_cache",
    "search_cache",
    "set_favorite",
    "set_played",
    "should_skip_uakino_in_fanout",
    "sources_cache",
    "user_state_store",
]
