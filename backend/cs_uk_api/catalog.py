"""Typed catalog interface (spec #309 step 2 / ticket #311).

The small typed seam the routes will import instead of reaching into
``catalog_state`` internals: cache keys, dict shapes and first-seen
ordering stop crossing the seam (US1/US2). This module only DELEGATES to
``catalog_state`` — the expand phase: every accessor preserves the
delegate's exact semantics, so existing callers keep working unchanged
(US11) and the step-3 migration moves them onto this surface without
behavior risk.

Accessor groups (~9):

  - snapshot: ``snapshot()`` (read) / ``refresh_snapshot()`` (build)
  - item resolution: ``resolve_item()`` — typed verdict, never a bare
    ``ContentResponse | None`` on the seam
  - search: ``search()`` — the group-registration step folded in (US3),
    so a searched card can never 404 via a missed manual call
  - playback: ``playback_positions()`` / ``recent_playback()`` — typed
    entries (no more ``dict[str, tuple[int, int | None]]`` on the seam);
    ``record_position()``; ``recent_history()``
  - viewer state: favorites/played + dub memory
  - profiles: ``profiles()`` (get) / ``refresh_profile()`` (install)

Cache-key construction is deliberately absent here — it stays inside
``catalog_state`` (the implementation), per the ticket's AC.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import catalog_state
from .models import (
    ContentResponse,
    HomeItem,
    HomeResponse,
    MediaForm,
    MediaStyle,
    SearchResponse,
    SearchResult,
)
from .recommend import ItemProfile


class ItemVerdict(str, Enum):
    """Typed verdict of a group-key resolution (US: no bare ``None`` on
    the seam, so callers can't confuse \"no content\" with a failure)."""

    OK = "ok"
    #: Cold cache / unknown key / gated / blocked / upstream failure —
    #: exactly the None answers the shared resolver gives, unchanged.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ItemResolution:
    """One group-key resolution: the verdict plus the content when OK."""

    verdict: ItemVerdict
    content: ContentResponse | None = None


@dataclass(frozen=True)
class PlaybackPosition:
    """One recorded playback entry — typed, where the seam used to hand
    out a bare ``(position_ticks, runtime_ticks)`` tuple."""

    position_ticks: int
    runtime_ticks: int | None


# ---------------------------------------------------------------------------
# Snapshot: read / refresh
# ---------------------------------------------------------------------------


def snapshot() -> HomeResponse | None:
    """The cached home snapshot, WITHOUT triggering a build.

    None on a cold cache (the same answer ``catalog_state.get_home``
    gives) — cheap reads (poster, similar shelf) must not fan out to
    every provider.
    """
    return catalog_state.get_home()


async def refresh_snapshot() -> HomeResponse:
    """The merged home snapshot, building it on a cache miss.

    The single load path behind both the native ``/api/home`` route and
    the facade: on a hit no provider is re-invoked; on a miss the
    persisted snapshot (ticket #269) serves instantly at any age while
    the full fan-out rebuilds in the background.
    """
    return await catalog_state.load_home()


# ---------------------------------------------------------------------------
# Item resolution: typed verdicts
# ---------------------------------------------------------------------------


async def resolve_item(group_key: str) -> ItemResolution:
    """Resolve a ``g2:`` group key to its content detail, typed.

    Delegates to the shared single-flight resolver (#224). The
    ``UNAVAILABLE`` verdict covers every None answer the delegate gives
    — cold cache, unknown key, gated, blocked, upstream failure — with
    identical semantics; callers match on ``verdict`` instead of testing
    ``content is None``.
    """
    content = await catalog_state.resolve_group_content(group_key)
    if content is not None:
        return ItemResolution(verdict=ItemVerdict.OK, content=content)
    return ItemResolution(verdict=ItemVerdict.UNAVAILABLE)


def peek_group_content(group_key: str) -> ContentResponse | None:
    """Cache-only group content read (ticket #216) — never fetches.

    The view-card Type is a cheap URL/section guess while the content
    page is the truth, and the facade re-verifies a card against this
    peek; a cold group answers None exactly like ``resolve_item`` would
    on its first cache-miss, so callers degrade identically ("keep the
    card's own guess") without paying a fetch.
    """
    return catalog_state.peek_group_content(group_key)


def is_hard_unavailable(group_key: str) -> bool:
    """True when a group key is DELIBERATELY unavailable — gated
    (subscription) or blocklisted (Russian content) — or unknown, as
    opposed to transiently unresolvable (an upstream blip).

    The facade detail route uses this to decide between a degraded card
    answer (a known card whose live resolution failed should still
    render) and the D2 404 — a gated/blocked verdict must NEVER be
    masked by the degradation.
    """
    return catalog_state.is_hard_unavailable(group_key)


def group_sources(group_key: str) -> list[SearchResult]:
    """Every card the resolution map holds for a ``g2:`` item, or [].

    First-seen provider order (the same order the home row's chip strip
    shows). The dict shape the delegate keeps internally stops crossing
    the seam — callers get the ordered card list directly.
    """
    per_provider = catalog_state.resolve_group(group_key)
    if per_provider is None:
        return []
    return list(per_provider.values())


def first_source(group_key: str) -> tuple[str, SearchResult] | None:
    """(provider_id, first-seen card) of a ``g2:`` group, or None.

    The first-seen provider is the same one the home row's chip strip
    shows and the detail resolver asks first — the derivation now lives
    behind the interface (US: the first-seen-provider ordering stops
    crossing the seam).
    """
    per_provider = catalog_state.resolve_group(group_key)
    if per_provider is None:
        return None
    return next(iter(per_provider.items()))


def episode_group_key(item_id: str) -> str | None:
    """The merged group key behind a played item id (spec #252).

    Movies report their ``g2:`` key; episodes report the provider-scoped
    wire id (``ufdub:dorama-408-...:s1e1``), whose ``provider:external``
    prefix identifies the merged group (reverse lookup, #214).
    """
    return catalog_state.episode_group_key(item_id)


# ---------------------------------------------------------------------------
# Deep rows (spec #305)
# ---------------------------------------------------------------------------


async def extend_row_pool(
    row_type: str,
    snapshot_items: list[HomeItem],
) -> list[HomeItem] | None:
    """The row's pool past the snapshot (spec #305), or None when bounded.

    Fetches provider browse pages 2..N for the row's contributing
    sections under the shared search budget (depth bounded by
    ``CS_UK_ROW_MAX_PAGES``), merges them with the home build's
    round-robin + group-key dedupe, and returns the snapshot items
    followed by the new deduped cards. None = the row kind does not
    extend (personalized / genre rails) or every deeper fetch failed —
    the caller then serves the snapshot slice unchanged.
    """
    return await catalog_state.extend_row_pool(row_type, snapshot_items)


# ---------------------------------------------------------------------------
# Search: registration folded in (US3)
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    provider: str = "all",
    form: MediaForm | None = None,
    style_filter: frozenset[MediaStyle] | None = None,
) -> SearchResponse:
    """Merged multi-provider search with group registration folded in.

    Runs the exact shared fan-out the native route uses (same
    per-provider failure attribution, gated sweep, uakino skip, 5-min
    cache — ADR-0003) and THEN registers the merged groups into the
    group-key resolution map, so a searched card opens in the detail
    surface without a separate manual ``register_search_groups`` call
    (US3: a missed registration can never 404 a searched card).
    """
    resp = await catalog_state.merged_search(
        query, provider=provider, form=form, style_filter=style_filter
    )
    catalog_state.register_search_groups(resp.groups)
    return resp


# ---------------------------------------------------------------------------
# Playback: typed entries
# ---------------------------------------------------------------------------


def playback_positions() -> Mapping[str, PlaybackPosition]:
    """Every recorded position, most-progressed first, typed.

    item_id -> PlaybackPosition. Same entries ``catalog_state`` holds;
    the bare tuple shape stops crossing the seam.
    """
    return {
        item_id: PlaybackPosition(position_ticks=pos, runtime_ticks=runtime)
        for item_id, (pos, runtime) in catalog_state.playback_entries().items()
    }


def recent_playback(limit: int = 20) -> Mapping[str, PlaybackPosition]:
    """Most recently updated positions, capped at ``limit`` — the resume
    rail's input, with the runtime for the wire bar (#250)."""
    return {
        item_id: PlaybackPosition(position_ticks=pos, runtime_ticks=runtime)
        for item_id, (pos, runtime) in catalog_state.recent_playback_entries(limit).items()
    }


def recent_history(limit: int = 20) -> list[str]:
    """Played item ids in most-recently-seen order, active AND finished —
    the «Нещодавно переглянуто» row's input (spec #272)."""
    return catalog_state.recent_history_entries(limit)


def record_position(
    item_id: str,
    position_ticks: int,
    *,
    runtime_ticks: int | None = None,
    flush: bool = False,
) -> None:
    """Record the client's playback position. Last report wins; zero
    positions are ignored; ``flush=True`` (the Stopped path) persists the
    state file synchronously."""
    catalog_state.record_playback(
        item_id, position_ticks, runtime_ticks=runtime_ticks, flush=flush
    )


# ---------------------------------------------------------------------------
# Viewer state: favorites / played / dub memory (spec #257, #276)
# ---------------------------------------------------------------------------


def is_favorite(item_id: str) -> bool:
    """True when the item is marked favorite (persisted user state)."""
    return catalog_state.is_favorite(item_id)


def set_favorite(item_id: str, is_favorite: bool) -> None:
    """Mark or unmark an item as favorite (spec #257)."""
    catalog_state.set_favorite(item_id, is_favorite)


def is_played(item_id: str) -> bool:
    """True when the item is marked played (persisted user state)."""
    return catalog_state.is_played(item_id)


def set_played(item_id: str, played: bool) -> None:
    """Mark or unmark an item as played (spec #257)."""
    catalog_state.set_played(item_id, played)


def remember_dub(series_group_key: str, translation_label: str) -> None:
    """Record the viewer's dub choice for a series (spec #276)."""
    catalog_state.remember_dub(series_group_key, translation_label)


def dub_for(series_group_key: str) -> str | None:
    """The remembered dub label for a series (spec #276), or None."""
    return catalog_state.dub_for(series_group_key)


def dub_memory() -> dict[str, str]:
    """The whole dub memory (group key -> label), for tests (spec #276)."""
    return catalog_state.dub_memory()


# ---------------------------------------------------------------------------
# Profiles: get / install (spec #252, #290)
# ---------------------------------------------------------------------------


def profiles() -> Mapping[str, ItemProfile]:
    """Read-only view of the warm content profiles (spec #252).

    Keyed by ``g2:`` group key; a cold store (empty) is the honest
    \"no signal yet\" answer — callers fall back, never branch on a
    sentinel.
    """
    return catalog_state.get_profiles()


async def refresh_profile(*, client: Any | None = None) -> bool:
    """One LLM taste-profile refresh (spec #290 user stories 10–12).

    Installs the active profile on success (True); on ANY failure the
    previous profile — or none — stays active and False is returned.
    Never raises. ``client`` is the injectable seam tests use.
    """
    return await catalog_state.refresh_profile(client=client)
