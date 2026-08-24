"""Typed catalog interface (spec #309 step 2 / ticket #311).

The small typed seam the routes will import instead of reaching into
``_catalog_state`` internals: cache keys, dict shapes and first-seen
ordering stop crossing the seam (US1/US2). This module only DELEGATES to
``_catalog_state`` — the expand phase: every accessor preserves the
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
``_catalog_state`` (the implementation), per the ticket's AC.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from . import _catalog_state
from ._catalog_state import PlaybackEpisodePairing
from .models import (
    ContentResponse,
    HomeItem,
    HomeResponse,
    MediaForm,
    MediaStyle,
    SearchResponse,
    SearchResult,
    Translation,
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

    None on a cold cache (the same answer ``_catalog_state.get_home``
    gives) — cheap reads (poster, similar shelf) must not fan out to
    every provider.
    """
    return _catalog_state.get_home()


async def refresh_snapshot() -> HomeResponse:
    """The merged home snapshot, building it on a cache miss.

    The single load path behind both the native ``/api/home`` route and
    the facade: on a hit no provider is re-invoked; on a miss the
    persisted snapshot (ticket #269) serves instantly at any age while
    the full fan-out rebuilds in the background.
    """
    return await _catalog_state.load_home()


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
    content = await _catalog_state.resolve_group_content(group_key)
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
    return _catalog_state.peek_group_content(group_key)


def is_hard_unavailable(group_key: str) -> bool:
    """True when a group key is DELIBERATELY unavailable — gated
    (subscription) or blocklisted (Russian content) — or unknown, as
    opposed to transiently unresolvable (an upstream blip).

    The facade detail route uses this to decide between a degraded card
    answer (a known card whose live resolution failed should still
    render) and the D2 404 — a gated/blocked verdict must NEVER be
    masked by the degradation.
    """
    return _catalog_state.is_hard_unavailable(group_key)


def group_sources(group_key: str) -> list[SearchResult]:
    """Every card the resolution map holds for a ``g2:`` item, or [].

    First-seen provider order (the same order the home row's chip strip
    shows). The dict shape the delegate keeps internally stops crossing
    the seam — callers get the ordered card list directly.
    """
    per_provider = _catalog_state.resolve_group(group_key)
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
    per_provider = _catalog_state.resolve_group(group_key)
    if per_provider is None:
        return None
    return next(iter(per_provider.items()))


def episode_group_key(item_id: str) -> str | None:
    """The merged group key behind a played item id (spec #252).

    Movies report their ``g2:`` key; episodes report the provider-scoped
    wire id (``ufdub:dorama-408-...:s1e1``), whose ``provider:external``
    prefix identifies the merged group (reverse lookup, #214).
    """
    return _catalog_state.episode_group_key(item_id)


def group_source(group_key: str, provider: str) -> SearchResult | None:
    """The resolution-map card of one ``g2:`` group for one provider, or
    None when the group or the provider is absent.

    The native source-switching route's lookup — replaces reaching into
    the ``{provider: SearchResult}`` dict on the seam.
    """
    per_provider = _catalog_state.resolve_group(group_key)
    if per_provider is None:
        return None
    return per_provider.get(provider)


class ContentVerdict(str, Enum):
    """Typed verdict of the native provider-scoped content path."""

    OK = "ok"
    GATED = "gated"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ProviderContent:
    """One provider-scoped content outcome: verdict plus the detail when
    OK. The route answers its own 404s from the verdict."""

    verdict: ContentVerdict
    content: ContentResponse | None = None


async def provider_content(provider_id: str, external_id: str) -> ProviderContent:
    """The native content-by-id path behind the seam (spec #309 T4).

    The cache layer, the gated/blocklist verdict stores and the
    stateless group-key derivation stay in the implementation — main no
    longer constructs ``content:`` keys or reads the verdict stores.
    Upstream ProviderError propagates to the caller's guard; a cached
    ``gated``/blocklisted verdict comes back typed so the native route
    answers its own 404s exactly as before.
    """
    verdict, content = await _catalog_state.cached_provider_content(
        provider_id, external_id
    )
    return ProviderContent(verdict=ContentVerdict(verdict), content=content)


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
    return await _catalog_state.extend_row_pool(row_type, snapshot_items)


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
    resp = await _catalog_state.merged_search(
        query, provider=provider, form=form, style_filter=style_filter
    )
    _catalog_state.register_search_groups(resp.groups)
    return resp


# ---------------------------------------------------------------------------
# Listing hygiene + uakino lifecycle (shared by the native routes)
# ---------------------------------------------------------------------------


#: Time budget for one subscription-gate sweep (the browse route wraps
#: ``filter_gated_items`` in ``asyncio.wait_for`` with this timeout; a
#: sweep timeout degrades to "keep the cards" — stream()/content() still
#: refuse gated items). Public name for the constant the implementation
#: keeps private (#345).
GATE_CHECK_TIMEOUT_S: float = _catalog_state.GATE_CHECK_TIMEOUT_S


async def filter_gated_items(
    items: Sequence[SearchResult], http: httpx.AsyncClient
) -> list[SearchResult]:
    """Subscription-gate sweep over one listing (can_gate providers).

    Drops cards whose only stream is the sponsor promo clip; verdicts
    are cached (gated/content stores) so a card is resolved once. Only
    KNOWN-gated cards are dropped — a transient upstream error keeps
    the card. The same sweep the shared home/search builds run; the
    browse route is just its only explicit caller.
    """
    return await _catalog_state.filter_gated_items(items, http)


async def await_uakino_ready() -> None:
    """Gate an explicit uakino route on the browser session being ready.

    A deterministic startup marker raises 502 — the session is dead for
    the process lifetime, so waiting is pointless. A session still
    warming raises 503 ``warming`` after the bounded wait so the client
    can back off and retry (issue #193/#196).
    """
    await _catalog_state.await_uakino_ready()


# ---------------------------------------------------------------------------
# Playback: typed entries
# ---------------------------------------------------------------------------


def playback_positions() -> Mapping[str, PlaybackPosition]:
    """Every recorded position, most-progressed first, typed.

    item_id -> PlaybackPosition. Same entries ``_catalog_state`` holds;
    the bare tuple shape stops crossing the seam.
    """
    return {
        item_id: PlaybackPosition(position_ticks=pos, runtime_ticks=runtime)
        for item_id, (pos, runtime) in _catalog_state.playback_entries().items()
    }


def recent_playback(limit: int = 20) -> Mapping[str, PlaybackPosition]:
    """Most recently updated positions, capped at ``limit`` — the resume
    rail's input, with the runtime for the wire bar (#250)."""
    return {
        item_id: PlaybackPosition(position_ticks=pos, runtime_ticks=runtime)
        for item_id, (pos, runtime) in _catalog_state.recent_playback_entries(limit).items()
    }


def recent_history(limit: int = 20) -> list[str]:
    """Played item ids in most-recently-seen order, active AND finished —
    the «Нещодавно переглянуто» row's input (spec #272)."""
    return _catalog_state.recent_history_entries(limit)


def flush_playback() -> None:
    """Flush pending playback state to disk (lifespan shutdown, #248)."""
    _catalog_state.flush_playback()


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
    _catalog_state.record_playback(
        item_id, position_ticks, runtime_ticks=runtime_ticks, flush=flush
    )


# ---------------------------------------------------------------------------
# Viewer state: favorites / played / dub memory (spec #257, #276)
# ---------------------------------------------------------------------------


def is_favorite(item_id: str) -> bool:
    """True when the item is marked favorite (persisted user state)."""
    return _catalog_state.is_favorite(item_id)


def set_favorite(item_id: str, is_favorite: bool) -> None:
    """Mark or unmark an item as favorite (spec #257)."""
    _catalog_state.set_favorite(item_id, is_favorite)


def is_played(item_id: str) -> bool:
    """True when the item is marked played (persisted user state)."""
    return _catalog_state.is_played(item_id)


def set_played(item_id: str, played: bool) -> None:
    """Mark or unmark an item as played (spec #257)."""
    _catalog_state.set_played(item_id, played)


def remember_dub(series_group_key: str, translation_label: str) -> None:
    """Record the viewer's dub choice for a series (spec #276)."""
    _catalog_state.remember_dub(series_group_key, translation_label)


def dub_for(series_group_key: str) -> str | None:
    """The remembered dub label for a series (spec #276), or None."""
    return _catalog_state.dub_for(series_group_key)


def dub_memory() -> dict[str, str]:
    """The whole dub memory (group key -> label), for tests (spec #276)."""
    return _catalog_state.dub_memory()


# ---------------------------------------------------------------------------
# Viewer-state derivations (#347): playback translations / dub choice /
# played-episode pairing — the domain half of the facade's viewer-state
# routes, folded off ``jellyfin/router.py``.
# ---------------------------------------------------------------------------


async def playback_translations(item_id: str) -> tuple[list[Translation], str | None]:
    """(candidate translations, remembered dub label) for a playable item
    (spec #276).

    The candidates come from the episode blob or the content page (never
    a second fetch); the remembered label is the series' dub memory.
    Movies are never remembered (v3 decision) — their label slot is
    always None.
    """
    return await _catalog_state.playback_translations(item_id)


def ordered_translation_candidates(
    translations: Sequence[Translation],
    *,
    remembered: str | None = None,
    picked_index: int | None = None,
) -> list[Translation]:
    """The picker's candidate order for one PlaybackInfo response
    (spec #276): dedupe by label (first provider wins), cap 8, then the
    echoed ``picked_index`` (1-based) — else the ``remembered`` dub —
    ranks first; otherwise default-first order stands."""
    return _catalog_state.ordered_translation_candidates(
        translations, remembered=remembered, picked_index=picked_index
    )


async def record_dub_choice(item_id: str, translation_id: str) -> None:
    """Record the viewer's dub pick as per-series memory (spec #276),
    best-effort: the id becomes its picker label, movies never remember,
    unresolvable items skip the memory."""
    await _catalog_state.record_dub_choice(item_id, translation_id)


async def playback_episode_pair(item_id: str) -> PlaybackEpisodePairing | None:
    """Locate a played episode wire id in its series (#347): the episode,
    its season, the series context and the next sibling — or None for a
    non-episode / unresolvable id."""
    return await _catalog_state.playback_episode_pair(item_id)


# ---------------------------------------------------------------------------
# Profiles: get / install (spec #252, #290)
# ---------------------------------------------------------------------------


def profiles() -> Mapping[str, ItemProfile]:
    """Read-only view of the warm content profiles (spec #252).

    Keyed by ``g2:`` group key; a cold store (empty) is the honest
    \"no signal yet\" answer — callers fall back, never branch on a
    sentinel.
    """
    return _catalog_state.get_profiles()


async def refresh_profile(*, client: Any | None = None) -> bool:
    """One LLM taste-profile refresh (spec #290 user stories 10–12).

    Installs the active profile on success (True); on ANY failure the
    previous profile — or none — stays active and False is returned.
    Never raises. ``client`` is the injectable seam tests use.
    """
    return await _catalog_state.refresh_profile(client=client)


def install_profiles(mapping: Mapping[str, ItemProfile]) -> None:
    """Install/replace the warm content profiles (spec #309 T4 install
    seam). Tests seed state through this instead of mutating the store's
    internals; the LLM warm installs through the same function (step 6).
    """
    _catalog_state.install_profiles(mapping)


def recommendation_stats() -> dict[str, int]:
    """Profile-store counts for the health surface (#253 AC5)."""
    return _catalog_state.recommendation_stats()
