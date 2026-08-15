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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx
from fastapi import HTTPException

from . import config as _config
from .cache import TtlCache
from .country import is_blocked_country
from .filters import matches_axes, style_key
from .health import TRACKER
from .home import build_genre_rows, build_home_rows, round_robin_dedup, section_row_type
from .http_client import get_client
from .llm import active_profile, fetch_profile, set_active_profile
from .merge import group_key_from, item_group_key, merge_results
from .models import (
    STATUS_DOWN,
    STATUS_WARMING,
    ContentResponse,
    ErrorResponse,
    HomeItem,
    HomeResponse,
    HomeRow,
    MediaForm,
    MediaStyle,
    ProviderFailure,
    SearchGroup,
    SearchResponse,
    SearchResult,
)
from .providers import PROVIDERS
from .providers.base import BaseProvider, ProviderError
from .recommend import (
    ANCHOR_WEIGHTS,
    MAX_ANCHORS,
    ItemProfile,
    build_recommendation_rows,
    profile_from_content,
)
from .resume_store import ResumeStore
from .snapshot_store import SnapshotStore
from .uakino_browser import get_session
from .user_state import UserStateStore
from .wire_identity import is_group_key, project_group, provider_union, split_episode_tail

log = logging.getLogger("cs_uk_api.catalog_state")

#: v3 (issue #70): the merged home view — «Новинки» + «Популярні зараз»
#: + the five type rows — is a curated snapshot, refreshed every 30 min.
home_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_home_s)

#: Multi-provider merged search (ticket #106): the native ``/api/search``
#: route and the Jellyfin facade share the SAME search cache (ADR-0003,
#: same 5m TTL as browse), so a query searched from either surface never
#: runs the provider fan-out twice. Key format and cache-key axes match
#: the route's contract exactly (``search:{provider}:{q}:{form}:{style}``).
search_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_search_s)

#: Bound on how long an explicit uakino route waits for the browser
#: session to become ready before answering 503 ``warming`` (issue #193).
#: Distinct from ``UakinoSession.WARM_TIMEOUT_S`` (the bounded ``warm()``
#: call itself). Lives here (not main.py) because ``await_uakino_ready``
#: is shared by the native routes and the facade search (ticket #106).
WARM_WAIT_S: float = 15.0

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


def _add_listing_to_sources_map(
    sources: dict[str, dict[str, SearchResult]], item: SearchResult
) -> None:
    """Fold one SearchResult into ``group_key → {provider → SearchResult}``.

    First-seen wins per (group_key, provider) — Python 3.7+ dict
    insertion order is preserved, so iteration order over the home
    listings determines the chip-strip / source order.
    """
    per_pid = sources.setdefault(item_group_key(item), {})
    per_pid.setdefault(item.provider, item)


def _build_sources_map(
    newest: Mapping[str, Sequence[SearchResult]],
    popular: Mapping[str, Sequence[SearchResult]],
    by_type: Mapping[str, Mapping[str, Sequence[SearchResult]]],
) -> dict[str, dict[str, SearchResult]]:
    """Build ``group_key → {provider → SearchResult}`` from raw listings.

    Iteration order matches ``build_home_rows``'s walk order
    (newest → popular → by_type), so the provider order seen here is
    the same first-seen order the home rows surface.

    Issue #161: home rows MERGE items across member keys (issue #89,
    yearful + yearless pairs) and surface the provider union under the
    canonical key. A per-item index alone hid non-canonical members
    from ``resolve_group``, so a chip the home row listed (e.g. klontv
    on a klontv+uakino merged card) 400'd via ``?source=``. Run the
    same merge and register the provider union under EVERY member key,
    so any key the client holds resolves the full group.
    """
    out: dict[str, dict[str, SearchResult]] = {}
    all_items: list[SearchResult] = []
    for source_map in (newest, popular):
        for items in source_map.values():
            for it in items:
                all_items.append(it)
                _add_listing_to_sources_map(out, it)
    for source_map in by_type.values():
        for items in source_map.values():
            for it in items:
                all_items.append(it)
                _add_listing_to_sources_map(out, it)
    for mg in merge_results(all_items):
        # Single projection (spec #309): canonical fields + member keys
        # + the first-seen provider union — no longer re-derived here.
        proj = project_group(mg)
        union = provider_union(proj.sources)
        for key in proj.member_keys:
            # Replace (not setdefault): the union is built from the same
            # first-seen walk order, so this keeps the exact provider
            # order the home row surfaces.
            out[key] = union
    return out


async def load_home() -> HomeResponse:
    """Return the merged home snapshot, building it on a cache miss.

    This is the single load path for BOTH the native ``/api/home`` route
    and the Jellyfin facade. On a hit no provider is re-invoked; on a
    miss the persisted snapshot (ticket #269) serves instantly at ANY
    age while the full fan-out rebuild runs in the background; with no
    persisted snapshot the build runs inline under the shared search
    budget (the pre-#269 cold-start behaviour).
    """
    cached = home_cache.get(_HOME_KEY)
    if cached is not None:
        return cast(HomeResponse, cached)

    store = _snapshot_store()
    persisted, sources = store.load()
    if persisted is not None:
        # Instant cold start: serve the stale snapshot immediately, heal
        # it in the background. Sources restored so group resolution for
        # the persisted rows works without any provider call.
        home_cache.set(_HOME_KEY, persisted)
        if sources is not None:
            sources_cache.set(_SOURCES_KEY, sources)
        asyncio.create_task(_build_home())
        return persisted
    return await _build_home()


async def _build_home() -> HomeResponse:
    """The full provider fan-out → rows → cache + persist (ticket #269).

    The heavy path behind ``load_home``: fetches every provider's
    newest/popular/type sections under the shared search budget, runs
    the subscription-gate sweep, and hands the collected listings to
    ``_cache_home``. On success the snapshot is persisted so the NEXT
    process start skips this cost entirely.
    """
    http = get_client()
    newest_lists: dict[str, list[SearchResult]] = {}
    popular_lists: dict[str, list[SearchResult]] = {}
    type_lists: dict[str, dict[str, list[SearchResult]]] = {}

    async def _newest(pid: str, section_id: str) -> None:
        try:
            results, _ = await PROVIDERS[pid].browse(section_id, 1, http)
        except Exception as e:  # noqa: BLE001
            log.warning("home newest skipped provider=%s err=%s", pid, e)
            TRACKER.record(pid, ok=False)
            return
        TRACKER.record(pid, ok=True)
        if results:
            newest_lists[pid] = list(results)

    async def _popular(pid: str, section_id: str) -> None:
        try:
            results, _ = await PROVIDERS[pid].browse(section_id, 1, http)
        except Exception as e:  # noqa: BLE001
            log.warning("home popular skipped provider=%s err=%s", pid, e)
            TRACKER.record(pid, ok=False)
            return
        TRACKER.record(pid, ok=True)
        if results:
            popular_lists[pid] = list(results)

    async def _type_section(pid: str, section_id: str, type_key: str) -> None:
        try:
            results, _ = await PROVIDERS[pid].browse(section_id, 1, http)
        except Exception as e:  # noqa: BLE001
            log.warning("home type skipped provider=%s section=%s err=%s", pid, section_id, e)
            TRACKER.record(pid, ok=False)
            return
        TRACKER.record(pid, ok=True)
        if results:
            buckets = type_lists.setdefault(type_key, {})
            buckets.setdefault(pid, []).extend(results)

    tasks: list[asyncio.Task[None]] = []
    for pid, provider in PROVIDERS.items():
        section_id = getattr(provider, "newest_section", None)
        if section_id:
            tasks.append(asyncio.create_task(_newest(pid, section_id)))
        if pid == "animeon" and provider.has_section("popular"):
            tasks.append(asyncio.create_task(_popular(pid, "popular")))
        for section in provider.sections:
            # Contract step #135: sections carry Model B axes, not the
            # legacy ``type`` — the home-row kind is derived from them.
            row_type = section_row_type(section)
            if row_type is not None:
                tasks.append(asyncio.create_task(_type_section(pid, section.id, row_type)))

    if tasks:
        # Bound the fan-out so a single hung provider can't drag the
        # whole /api/home request out to ``upstream_timeout_s * N``. The
        # 30-min home cache absorbs the steady-state latency.
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=False),
                timeout=_config.SETTINGS.search_total_timeout_s,
            )
        except TimeoutError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.warning("home fan-out hit overall budget providers=%d", len(tasks))

    # Subscription-gate sweep (BambooUA "Для підписників"): a card whose
    # only stream is the sponsor promo clip is resolved once and dropped
    # BEFORE the rows and the sources map are built, so the promo never
    # surfaces as a playable card — and a merged title keeps its
    # working sources (the gated provider just stops contributing).
    http = get_client()

    async def _sweep(mapping: dict[str, list[SearchResult]], pid: str) -> None:
        mapping[pid] = await filter_gated_items(mapping[pid], http, sem=sweep_sem)

    # Only can_gate providers need the sweep (the filter is a no-op for
    # everyone else) — and only when their listing is non-empty.
    # Issue #168: the sweep spawns one task per (listing, provider) — up
    # to a dozen — and each task's filter used to create its OWN
    # concurrency semaphore, so total upstream concurrency was
    # ~N×24 simultaneous requests. Upstreams throttle that into
    # slowness, the sweep blew its budget, and gated cards leaked into
    # home. ONE shared semaphore bounds the whole sweep instead.
    sweep_sem = asyncio.Semaphore(_GATE_CHECK_CONCURRENCY)
    sweep: list[asyncio.Task[None]] = []
    for mapping in (newest_lists, popular_lists):
        for pid, items in list(mapping.items()):
            maybe_provider = PROVIDERS.get(pid)
            if items and maybe_provider is not None and maybe_provider.can_gate:
                sweep.append(asyncio.create_task(_sweep(mapping, pid)))
    for per_pid in type_lists.values():
        for pid, items in list(per_pid.items()):
            maybe_provider = PROVIDERS.get(pid)
            if items and maybe_provider is not None and maybe_provider.can_gate:
                sweep.append(asyncio.create_task(_sweep(per_pid, pid)))
    if sweep:
        _done, pending = await asyncio.wait(sweep, timeout=_GATE_CHECK_TIMEOUT_S)
        if pending:
            # Issue #168: a cold sweep of every can_gate listing can
            # outlive the budget (animeon's content() does several
            # upstream hops per item). DON'T cancel the stragglers —
            # let them finish in the background and re-cache the
            # cleaned home, so the gated cards disappear seconds later
            # instead of leaking into the 30-min snapshot. The first
            # caller gets the fast (partially-swept) home.
            async def _finish_sweep(tasks: set[asyncio.Task[None]]) -> None:
                await asyncio.wait(tasks)
                resp = _cache_home(newest_lists, popular_lists, type_lists)
                if _config.SETTINGS.catalog_warm_enabled:
                    asyncio.create_task(_warm_profiles(resp))

            asyncio.create_task(_finish_sweep(pending))

    resp = _cache_home(newest_lists, popular_lists, type_lists)
    # Recommendation profiles warm in the background (spec #252): the
    # same bounded-concurrency pattern as the gate sweep, piggybacking
    # the content cache. Gated on ``catalog_warm_enabled`` so tests
    # (which disable it) never trigger real content scrapes.
    if _config.SETTINGS.catalog_warm_enabled:
        asyncio.create_task(_warm_profiles(resp))
    return resp


def _watched_group_keys() -> set[str]:
    """Every group behind a recorded playback position (spec #267 T3).

    The same resolution the NextUp shelf uses: an episode wire id's
    ``provider:external`` prefix identifies its merged group.
    """
    return {
        gk
        for item_id in playback_entries()
        if (gk := episode_group_key(item_id)) is not None
    }


def _history_group_keys(limit: int = 20) -> list[str]:
    """Ordered group keys of the playback history (spec #272), most
    recent first, active AND finished — the «Нещодавно переглянуто»
    row's input."""
    resolved: list[str] = []
    seen: set[str] = set()
    for item_id in recent_history_entries(limit):
        gk = episode_group_key(item_id)
        if gk is None or gk in seen:
            continue
        seen.add(gk)
        resolved.append(gk)
    return resolved


def _cache_home(
    newest: Mapping[str, Sequence[SearchResult]],
    popular: Mapping[str, Sequence[SearchResult]],
    type_lists: Mapping[str, Mapping[str, Sequence[SearchResult]]],
) -> HomeResponse:
    """Build the rows from the collected listings; cache + persist.

    The single place a successful home build lands: computes the
    personalized rows (spec #252) and the «Нові серії» row (spec #267
    T3, from the playback store's watched groups), caches the snapshot
    and the group resolution map, and persists both to the versioned
    snapshot file (ticket #269) so the next cold start serves instantly.
    """
    rows = _with_recommendation_rows(
        build_home_rows(
            newest=newest,
            popular=popular,
            by_type=type_lists,
            newest_limit=_config.SETTINGS.home_row_limit,
            watched_series=_watched_group_keys(),
            history_groups=_history_group_keys(),
        )
    )
    resp = HomeResponse(rows=rows)
    home_cache.set(_HOME_KEY, resp)
    # A new snapshot invalidates the deep-row pools (spec #305): they
    # are anchored to the snapshot's page-1 items, so a rebuild must
    # not serve a pool deduped against the previous snapshot.
    row_deep_cache.clear()
    sources = _build_sources_map(newest, popular, type_lists)
    sources_cache.set(_SOURCES_KEY, sources)
    _snapshot_store().save(resp, sources)
    return resp


def get_home() -> HomeResponse | None:
    """Cached home snapshot without triggering a build (None on cold cache)."""
    return cast(HomeResponse, home_cache.get(_HOME_KEY))


# ---------------------------------------------------------------------------
# Deep rows (spec #305): lazy pagination of home rows
# ---------------------------------------------------------------------------

#: Row kinds whose pool extends past the snapshot when the client
#: scrolls (spec #305): the five type rows, the two form-split
#: «Нещодавно додані» rows, and «Популярні зараз». The personalized
#: rows («Нові серії», «Нещодавно переглянуто», «Рекомендовано для
#: тебе», «Схоже на X») and the genre rails stay snapshot-bounded by
#: design (their pool IS the snapshot).
_EXTENDABLE_ROWS = frozenset(
    {
        "movie",
        "series",
        "anime",
        "cartoon",
        "dorama",
        "recent_movie",
        "recent_series",
        "popular",
    }
)

#: Form filter for the form-split recent rows: a movie-form recent row
#: must never pick up series cards from a provider's deeper newest
#: pages (mirrors the page-1 build's per-form filter).
_RECENT_ROW_FORM = {"recent_movie": "movie", "recent_series": "series"}


def _row_sources(row_type: str) -> list[tuple[str, str]]:
    """(provider_id, section_id) pairs whose deeper pages extend a row kind.

    Mirrors the home build's page-1 sources: the five type rows come
    from every provider section whose Model B axes map to that kind
    (``home.section_row_type``); the form-split recent rows come from
    the providers' ``newest_section`` (their page-1 source — the form
    filter applies at item level); «Популярні зараз» comes from the
    ``popular`` section of whichever provider holds the role.
    """
    sources: list[tuple[str, str]] = []
    for pid, provider in PROVIDERS.items():
        if row_type in _RECENT_ROW_FORM:
            if provider.newest_section is not None:
                sources.append((pid, provider.newest_section))
        elif row_type == "popular":
            if provider.has_section("popular"):
                sources.append((pid, "popular"))
        else:
            for section in provider.sections:
                if section_row_type(section) == row_type:
                    sources.append((pid, section.id))
    return sources


def _deep_key(row_type: str) -> str:
    return f"row-deep:{row_type}"


async def extend_row_pool(
    row_type: str,
    snapshot_items: Sequence[HomeItem],
) -> list[HomeItem] | None:
    """The row's pool past the snapshot (spec #305), or None when bounded.

    Called by the facade's Items route when the client requests a page
    beyond the snapshot row. Fetches provider browse pages 2..N for the
    row's contributing sections under the shared search budget (depth
    bounded by ``CS_UK_ROW_MAX_PAGES``, default 5 ≈ 100 cards per row),
    merges them with the same round-robin + group-key dedupe the home
    build uses, and returns the snapshot items followed by the new
    deduped cards.

    None = the row kind does not extend (personalized / genre rails), no
    contributing sections, or every deeper fetch failed — the caller
    then serves the snapshot slice unchanged (graceful degradation).
    The extended pool caches per row kind with the browse-cache TTL and
    is cleared when the home snapshot rebuilds.
    """
    if row_type not in _EXTENDABLE_ROWS or not snapshot_items:
        return None
    cached = row_deep_cache.get(_deep_key(row_type))
    if cached is not None:
        return cast(list[HomeItem], cached)
    sources = _row_sources(row_type)
    if not sources:
        return None
    max_pages = max(_config.SETTINGS.row_max_pages, 1)
    form = _RECENT_ROW_FORM.get(row_type)
    http = get_client()
    collected: dict[str, list[SearchResult]] = {}

    async def _fetch(pid: str, section_id: str, page: int) -> None:
        cache_key = f"deep:{pid}:{section_id}:{page}"
        cached_page = deep_page_cache.get(cache_key)
        if cached_page is None:
            try:
                results, _ = await PROVIDERS[pid].browse(section_id, page, http)
            except Exception as e:  # noqa: BLE001 — one failing page degrades, never breaks
                log.warning(
                    "deep row fetch skipped provider=%s section=%s page=%d err=%s",
                    pid,
                    section_id,
                    page,
                    e,
                )
                TRACKER.record(pid, ok=False)
                return
            TRACKER.record(pid, ok=True)
            deep_page_cache.set(cache_key, list(results))
        else:
            results = cast(list[SearchResult], cached_page)
        if form is not None:
            results = [r for r in results if r.form == form]
        if results:
            collected.setdefault(pid, []).extend(results)

    tasks = [
        asyncio.create_task(_fetch(pid, section_id, page))
        for pid, section_id in sources
        for page in range(2, max_pages + 1)
    ]
    if not tasks:
        return None
    # Bound the fan-out the same way the home build does: one hung
    # provider page must not drag the scroll request past the shared
    # search budget.
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=False),
            timeout=_config.SETTINGS.search_total_timeout_s,
        )
    except TimeoutError:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.warning("deep row extension hit overall budget row=%s sources=%d", row_type, len(sources))

    if not any(collected.values()):
        return None  # every deeper fetch failed or ran dry → snapshot slice

    raw_total = sum(len(v) for v in collected.values())
    deeper = round_robin_dedup(collected, limit=raw_total)
    seen = {it.group_key for it in snapshot_items}
    pool = list(snapshot_items)
    for it in deeper:
        if it.group_key in seen:
            continue
        pool.append(it)
        seen.add(it.group_key)
    row_deep_cache.set(_deep_key(row_type), pool)
    return pool


def _snapshot_store() -> SnapshotStore:
    """The module-level snapshot store (memory-only in the test suite)."""
    return _snapshot_store_ref


def clear_snapshot_store() -> None:
    """Re-instantiate the store from the current ``SETTINGS.snapshot_path``
    (tests that flip the path knob)."""
    global _snapshot_store_ref
    _snapshot_store_ref = SnapshotStore(_config.SETTINGS.snapshot_path)


_snapshot_store_ref: SnapshotStore = SnapshotStore(_config.SETTINGS.snapshot_path)


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


# ---------------------------------------------------------- recommendations (#252)

#: Content-page taste profiles of the home-snapshot groups (spec #252),
#: keyed by ``g2:`` group key. Built in the background by
#: ``_warm_profiles``; in-memory only — a restart re-warms (bounded by
#: the content cache).
_profiles: dict[str, ItemProfile] = {}

#: Bounded concurrency for the background profile warm (spec #252: the
#: same bounded-concurrency pattern as the gate sweep).
_PROFILE_CONCURRENCY = 8

def episode_group_key(item_id: str) -> str | None:
    """The merged group key behind a played item id (spec #252).

    Movies report their ``g2:`` key; episodes report the provider-scoped
    wire id (``ufdub:dorama-408-...:s1e1``), whose ``provider:external``
    prefix identifies the merged group (reverse lookup, #214). The
    tail grammar lives in ``wire_identity`` (spec #309).
    """
    if is_group_key(item_id):
        return item_id
    split = split_episode_tail(item_id)
    if split is None:
        return None
    composite, _tail = split
    return group_key_for_external(composite)


async def _warm_profiles(home: HomeResponse) -> None:
    """Background content-profile build for the home groups (spec #252).

    Bounded concurrency, piggybacking the shared content cache — only
    cold groups cost a fetch. On completion, if any NEW profile landed,
    the home cache is invalidated so the next read rebuilds the
    snapshot WITH the recommendation rows (they are computed at build
    time). A warm that adds nothing (steady state) never invalidates,
    so the rebuild→warm loop terminates. A failed profile is just a
    missing signal — never an error.
    """
    groups = sorted({it.group_key for row in home.rows for it in row.items})
    if not groups:
        return
    sem = asyncio.Semaphore(_PROFILE_CONCURRENCY)
    added = False

    async def _one(group_key: str) -> None:
        nonlocal added
        if group_key in _profiles:
            return
        per_provider = resolve_group(group_key)
        if per_provider is None:
            return
        provider_id, item = next(iter(per_provider.items()))
        cache_key = _gate_cache_key(item)
        if gated_cache.get(cache_key) is True:
            return
        cached = content_cache.get(cache_key)
        if cached is None:
            provider = PROVIDERS.get(provider_id)
            if provider is None:
                return
            async with sem:
                try:
                    _, _, external = item.id.partition(":")
                    cached = await provider.content(external, get_client())
                    content_cache.set(cache_key, cached)
                except Exception as e:  # noqa: BLE001 — a failed profile is just a missing signal
                    log.debug("profile warm failed group=%s err=%s", group_key, e)
                    return
        _profiles[group_key] = profile_from_content(cast(ContentResponse, cached))
        added = True

    tasks = [asyncio.create_task(_one(gk)) for gk in groups]
    await asyncio.wait(tasks, timeout=_config.SETTINGS.search_total_timeout_s)
    if added:
        home_cache.clear()
        # The next home rebuild changes the snapshot — drop the
        # snapshot-anchored deep pools with it (spec #305).
        row_deep_cache.clear()


def _recommendation_rows(rows: Sequence[HomeRow]) -> list[HomeRow]:
    """«Рекомендовано для тебе» + «Схоже на X» from the current taste
    signal (spec #252).

    Candidates are the snapshot's groups with a warm profile, excluding
    already-watched groups; anchors are the up-to-3 most recently
    watched items (recency-weighted); the similar row anchors on the
    single most recent in-progress title. Rows are omitted when there
    is no signal (no anchors, no queries) — empty rows don't ship.
    """
    home_items = [it for row in rows for it in row.items]
    if not home_items or not _profiles:
        return []
    home_by_key = {it.group_key: it for it in home_items}
    # Excluded set (#253 AC4): EVERY group behind a recorded playback
    # position is off the recommendation shelves — not just the few that
    # also anchor the taste profile.
    watched = {
        gk
        for item_id in playback_entries()
        if (gk := episode_group_key(item_id)) is not None
    }
    anchors: list[tuple[ItemProfile, float]] = []
    similar: tuple[ItemProfile, str] | None = None
    recency = 0
    for item_id in recent_playback_entries(MAX_ANCHORS):
        group_key = episode_group_key(item_id)
        if group_key is None:
            continue
        prof = _profiles.get(group_key)
        if prof is None:
            continue
        if recency < len(ANCHOR_WEIGHTS):
            anchors.append((prof, ANCHOR_WEIGHTS[recency]))
        recency += 1
        item = home_by_key.get(group_key)
        if similar is None and item is not None:
            similar = (prof, item.title)
    queries = recent_search_queries()
    # An active profile's idea rows are genre signal alone — they can
    # ship without any anchor/query taste signal (spec #290 user story
    # 5: the curated rows exist even when the viewer hasn't watched or
    # searched recently).
    profile = active_profile()
    if not anchors and not queries and not (profile and profile.row_ideas):
        return []
    return build_recommendation_rows(
        home_items=home_items,
        profiles=_profiles,
        watched=watched,
        anchors=anchors,
        similar_anchor=similar,
        queries=queries,
        profile=profile,
    )


def _with_recommendation_rows(rows: list[HomeRow]) -> list[HomeRow]:
    """Insert the recommendation rows after «Популярні зараз» (or the
    form-split recent rows when popular is absent), before the type
    rows (#252) — and append the genre rails (spec #263) at the end.

    Both personalized families need warm content profiles; with none
    they are simply omitted (no signal → no rows).
    """
    rec = _recommendation_rows(rows)
    out = list(rows)
    if rec:
        insert_at = 0
        for i, row in enumerate(out):
            if row.type in ("newest", "recent_movie", "recent_series", "popular"):
                insert_at = i + 1
        out[insert_at:insert_at] = rec
    genre = build_genre_rows(
        home_items=[it for row in out for it in row.items],
        profiles=_profiles,
    )
    if genre:
        out.extend(genre)
    return out


# ---------------------------------------------------- LLM wiring (spec #290)


def _llm_history_signal() -> list[dict[str, object]]:
    """The up-to-10 most recent history items as taste signals.

    Each entry is {title, genres, year, form} for the LLM: the item id
    resolves to its merged group key through the series-group reverse
    lookup (episodes report ``provider:external:s1e1`` wire ids), and
    only groups with a warm content profile contribute — a cold group
    has no taste to signal. Titles come from the current home snapshot
    (profiles carry no title); a group outside the snapshot falls back
    to its group key.
    """
    home = get_home()
    title_by_key: dict[str, str] = {}
    if home is not None:
        title_by_key = {it.group_key: it.title for row in home.rows for it in row.items}
    out: list[dict[str, object]] = []
    for item_id in recent_history_entries(10):
        group_key = episode_group_key(item_id)
        if group_key is None:
            continue
        prof = _profiles.get(group_key)
        if prof is None:
            continue
        out.append(
            {
                "title": title_by_key.get(group_key, group_key),
                "genres": sorted(prof.genres),
                "year": prof.year,
                "form": prof.form,
            }
        )
    return out


async def refresh_profile(*, client: Any | None = None) -> bool:
    """One LLM taste-profile refresh (spec #290 user stories 10–12).

    Collects the signals (recent history via the series-group reverse
    lookup, recent queries, the profile-derived catalog genre
    vocabulary), calls the model, and installs the active profile on
    success. Returns True only when a profile was installed; on ANY
    failure (missing knobs, network error, invalid answer) the previous
    profile — or none — stays active and False is returned. Never
    raises, never blocks the home build: the LLM call happens here, not
    on the home path. ``client`` is the injectable seam (tests pass a
    fake; production uses the configured endpoint).
    """
    try:
        profile = await fetch_profile(
            history=_llm_history_signal(),
            queries=recent_search_queries(),
            genres=sorted({g for p in _profiles.values() for g in p.genres}),
            client=client,
        )
    except Exception as e:  # noqa: BLE001 — the layer degrades to inert
        log.warning("llm taste-profile refresh failed: %s", e)
        return False
    if profile is None:
        return False
    set_active_profile(profile)
    # The home rows are BUILT from the active profile — the new weights/
    # tags/ideas must surface on the next build, not after the 30-min
    # home TTL. Clearing only invalidates; the next request rebuilds.
    home_cache.clear()
    return True


#: Cap on concurrent content-page fetches during the gate sweep, and
#: the sweep's own time budget. The budget deliberately lives OUTSIDE
#: the 12s search fan-out: a sweep timeout degrades to "keep the cards"
#: (stream()/content() still refuse gated items, so the promo clip
#: never plays) rather than failing the whole home build. Issue #168:
#: the inline budget is deliberately short — a cold sweep of every
#: can_gate listing can take 20-60s (animeon's content() does several
#: upstream hops per item), so tasks that outlive it are NOT cancelled:
#: load_home returns the fast partially-swept home and a background
#: task finishes the sweep and re-caches the cleaned home seconds
#: later. The 30-min home cache absorbs the re-cache.
_GATE_CHECK_CONCURRENCY = 24
_GATE_CHECK_TIMEOUT_S = 12.0

#: Delay before the one detail-scrape retry (B23): providers flake once
#: under load (animeon ``unreachable``) and succeed on a second attempt.
CONTENT_RETRY_DELAY_S = 1.0


def _gate_cache_key(item: SearchResult) -> str:
    """The shared ``content:{provider}:{external}`` key for a card."""
    _, _, external = item.id.partition(":")
    return f"content:{item.provider}:{external}"


async def filter_gated_items(
    items: Sequence[SearchResult],
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore | None = None,
) -> list[SearchResult]:
    """Drop subscription-gated sources from a listing (can_gate providers).

    Gating is only knowable from the item's content page — the site
    never marks listings — so every card of a ``can_gate`` provider is
    resolved once. Verdicts are cached (``gated_cache``, TTL > home
    cache) and the shared ``content_cache`` is populated with the same
    shape the detail routes use, so the sweep is free on every later
    rebuild and a resolved detail page never double-fetches.

    Only KNOWN-gated items are dropped; a transient upstream error
    keeps the card (dead providers are health-tracked elsewhere, and
    ``stream()`` still refuses gated items on its own).
    """
    todo = [it for it in items if it.provider in PROVIDERS and PROVIDERS[it.provider].can_gate]
    if not todo:
        return list(items)
    # Issue #168: callers may pass a shared semaphore so parallel sweep
    # tasks (one per listing/provider) don't each open their own
    # concurrency window — N tasks × 24 requests each throttled the
    # upstreams and blew the sweep budget, leaking gated cards into home.
    if sem is None:
        sem = asyncio.Semaphore(_GATE_CHECK_CONCURRENCY)

    async def check(item: SearchResult) -> bool:
        """True iff ``item`` is KNOWN gated (cached or freshly resolved)."""
        key = _gate_cache_key(item)
        cached = gated_cache.get(key)
        if cached is not None:
            return bool(cached)
        if content_cache.get(key) is not None:
            gated_cache.set(key, False, ttl_s=_config.SETTINGS.cache_content_s)
            return False
        async with sem:
            # Re-check under the lock: another sweep may have resolved
            # the same card while we waited for the semaphore.
            cached = gated_cache.get(key)
            if cached is not None:
                return bool(cached)
            if content_cache.get(key) is not None:
                gated_cache.set(key, False, ttl_s=_config.SETTINGS.cache_content_s)
                return False
            provider = PROVIDERS[item.provider]
            _, _, external = item.id.partition(":")
            try:
                resp = await provider.content(external, http)
            except ProviderError as e:
                if e.code == "gated":
                    gated_cache.set(key, True)
                    return True
                return False
            except Exception:  # noqa: BLE001
                return False
            # Mirror the detail routes' cache shape (group_key set
            # BEFORE caching, ADR-0003) + the blocklist check, so a
            # later detail cache-hit behaves identically.
            if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
                blocklist_cache.set(key, True)
                return False
            resp.group_key = group_key_from(resp.title, resp.form, resp.year, resp.id)
            content_cache.set(key, resp)
            # A known-good verdict follows the CONTENT TTL (not the long
            # gated TTL): an un-gated title is re-checked when its
            # content cache expires, so it re-enters the catalog as soon
            # as the upstream publishes the real video.
            gated_cache.set(key, False, ttl_s=_config.SETTINGS.cache_content_s)
            return False

    tasks = {asyncio.create_task(check(it)): it for it in todo}
    gated_ids: set[str] = set()
    # Issue #168: run to COMPLETION, never cancel pending checks. The
    # load_home sweep bounds the whole pass with its own budget and
    # finishes stragglers in the background — but if THIS function
    # cancelled its checks on timeout it would return a partially-
    # filtered list and gated cards would leak into the cached home.
    done, _pending = await asyncio.wait(tasks.keys())
    for t in done:
        item = tasks[t]
        try:
            if t.result():
                gated_ids.add(item.id)
        except Exception:  # noqa: BLE001, S110
            pass
    return [it for it in items if it.id not in gated_ids]


def resolve_group(group_key: str) -> dict[str, SearchResult] | None:
    """Resolution for a ``g2:`` group key (ticket #101).

    Returns the ``provider -> SearchResult`` map for the group, or
    ``None`` when the key is absent (cold cache → the caller yields a
    404 "item unavailable", which Jellyfin clients tolerate).
    """
    per_provider: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    return per_provider.get(group_key)


def group_key_for_external(composite: str) -> str | None:
    """Reverse group lookup: ``provider:external`` -> its ``g2:`` key.

    The playback reports carry the item id the client played — for an
    episode that is the provider-scoped wire id
    (``ufdub:dorama-408-...:s1e1``), whose ``provider:external`` prefix
    identifies the merged group (ticket #214). Built from the same
    ``sources_cache`` map ``resolve_group`` reads.

    Ticket #234: the episode prefix is NOT always the card's composite
    id. uakino's episode wire id carries only the bare numeric news id
    (``uakino:6268:e1``) while its search card id is the full
    ``uakino:anime-series:6268-narutto-1-sezon`` — the exact match
    misses and the resume rail would drop the episode. Fall back to a
    same-provider item-id match: the numeric segment (``6268``) appears
    as the ``{section}:{item_id}-{slug}`` shape of one of the group's
    cards.
    """
    per_provider: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    for group_key, providers in per_provider.items():
        for result in providers.values():
            if result.id == composite:
                return group_key
    provider_id, _, item_seg = composite.partition(":")
    if not provider_id or not item_seg.isdigit():
        return None
    for group_key, providers in per_provider.items():
        for result in providers.values():
            if result.provider != provider_id:
                continue
            # ``{section}:{item_id}-{slug}`` (uakino) or ``{item_id}``
            # (animeon) — the numeric id is a segment boundary, never
            # part of a longer number.
            for part in result.id.split(":"):
                if part == item_seg or part.startswith(f"{item_seg}-"):
                    return group_key
    return None


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
    with the runtime for the wire bar (#250)."""
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


def recommendation_stats() -> dict[str, int]:
    """Profile-store counts for the health surface (#253 AC5).

    Profiles warmed, search queries recorded, groups with a recorded
    playback position (the recommendation exclusion set) — all
    debuggable from the existing ``/api/health``, no new endpoint.
    """
    return {
        "profiles": len(_profiles),
        "queries": len(recent_search_queries()),
        "watched": len(playback_entries()),
    }


def peek_group_content(group_key: str) -> ContentResponse | None:
    """Cache-only group content read (ticket #216).

    Returns the first-seen provider's ContentResponse when it is already
    cached — never fetches. The view-card Type is a cheap
    URL/section-guess while the content page is the truth, and the
    facade re-verifies a card against this peek; a cold group answers
    None exactly like ``resolve_group_content`` would on its first
    cache-miss, so callers degrade identically ("keep the card's own
    guess") without paying a fetch.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return None
    provider_id, item = next(iter(per_provider.items()))
    _, _, external_id = item.id.partition(":")
    cache_key = f"content:{provider_id}:{external_id}"
    if gated_cache.get(cache_key) is True:
        return None
    if blocklist_cache.get(cache_key) is not None:
        return None
    cached = content_cache.get(cache_key)
    if cached is None:
        return None
    return cast(ContentResponse, cached)


#: Single-flight guard for ``resolve_group_content`` (ticket #224): a
#: per-cache-key task so concurrent facade calls for the SAME item — the
#: app fires detail + seasons + episodes + playback in one tick — share
#: ONE upstream resolution and its verdict instead of N parallel
#: re-resolves (run8 re-hit animeon 4x in ~2s during a 502 storm; run7's
#: cold walk ballooned to 59 fetches across two retry rounds). The map
#: is bounded: each entry is removed once its task finishes.
_resolve_inflight: dict[str, asyncio.Task[ContentResponse | None]] = {}


async def resolve_group_content(group_key: str) -> ContentResponse | None:
    """Resolve a ``g2:`` group key to ONE provider's content detail.

    The facade's ticket #105 detail path: a ``g2:`` key maps to the same
    ``{provider: SearchResult}`` map the native ``/api/home`` populates;
    the first-seen provider (same order the home row's chip strip shows)
    is asked for its ContentResponse. The response comes from the SAME
    ``content_cache`` / ``blocklist_cache`` stores the native
    ``/api/content`` route uses — one TTL, one cache-key shape, one
    ``clear()`` — so a detail view and a native content call never cache
    two different shapes of the same title.

    Returns ``None`` when the key is absent (cold cache, D2's "item
    unavailable" 404) or the provider's ``content()`` raises (the facade
    degrades to 404; it never surfaces a 502 like a native route would —
    Jellyfin clients treat both as "skip this item").

    Single-flight (#224): concurrent callers for the same key wait for
    the first resolution and share its verdict (success OR failure) — a
    failed leader means the waiters return None without re-storming the
    upstream.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return None
    # First-seen provider; the SearchResult's composite id
    # (``provider:external``) is split back to the bare external id the
    # provider's content() expects (animeon rejects composite ids).
    provider_id, item = next(iter(per_provider.items()))
    _, _, external_id = item.id.partition(":")
    cache_key = f"content:{provider_id}:{external_id}"
    # Single-flight (#224): if a resolution for this key is already
    # in flight, share ITS verdict (success or failure) instead of
    # re-storming the upstream in parallel — the app fires detail +
    # seasons + episodes + playback for one item in the same tick, and
    # duplicate walks are what stretched run7's cold resolution and
    # multiplied run8's 502s. The map entry is created synchronously
    # (no await between the get and the set), so two callers in the
    # same event-loop tick can never both become leader.
    existing = _resolve_inflight.get(cache_key)
    if existing is not None:
        return await existing
    task = asyncio.create_task(
        _resolve_group_content_once(
            cache_key, group_key, provider_id, external_id,
        )
    )
    _resolve_inflight[cache_key] = task
    try:
        return await task
    finally:
        # Drop the guard once the burst is over (bounded memory); a
        # later, genuinely new request starts a fresh resolution.
        if _resolve_inflight.get(cache_key) is task:
            _resolve_inflight.pop(cache_key, None)


async def _resolve_group_content_once(
    cache_key: str,
    group_key: str,
    provider_id: str,
    external_id: str,
) -> ContentResponse | None:
    """One leader resolution for ``cache_key`` (single-flight, #224).

    Re-checks the caches under the guard: a previous burst may have
    landed a verdict between the caller's first check and the task
    starting. Mirrors the body of the pre-#224 ``resolve_group_content``.
    """
    if gated_cache.get(cache_key) is True:
        return None
    if blocklist_cache.get(cache_key) is not None:
        return None
    cached = content_cache.get(cache_key)
    if cached is not None:
        return cast(ContentResponse, cached)
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return None
    http = get_client()
    resp = None
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = await provider.content(external_id, http)
            break
        except ProviderError as e:
            if e.code == "gated":
                # ADR-0002: a gated verdict never moves the health
                # tracker. Cache it so every later group-key call
                # short-circuits — the load_home sweep normally
                # populates `gated_cache` first, but a cold-cache g2:
                # detail call must not record a health-down here either
                # (#139).
                gated_cache.set(cache_key, True)
                return None
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt == 1:
            # B23: heavy titles flake once under load (animeon
            # ``unreachable``) and then succeed — retry before declaring
            # the item unavailable, so the runner warm / app detail don't
            # 404 a valid item. The health tracker only sees the verdict.
            log.warning(
                "group content failed provider=%s key=%s attempt 1/2 err=%s — retrying",
                provider_id, group_key, last_err,
            )
            await asyncio.sleep(CONTENT_RETRY_DELAY_S)
    if resp is None:
        log.warning(
            "group content failed provider=%s key=%s both attempts err=%s",
            provider_id, group_key, last_err,
        )
        TRACKER.record(provider_id, ok=False)
        return None
    TRACKER.record(provider_id, ok=True)
    if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
        blocklist_cache.set(cache_key, True)
        log.info("blocked Russian content id=%s country=%s", cache_key, resp.country)
        return None
    resp.group_key = group_key
    content_cache.set(cache_key, resp)
    return resp


async def cached_provider_content(
    provider_id: str, external_id: str
) -> tuple[str, ContentResponse | None]:
    """The native content-by-id cache path (spec #309 T4), verdict first.

    Mirrors the pre-T4 ``main._content_by_id`` semantics exactly, with
    the cache key and verdict stores staying HERE (the implementation):

      - blocklisted → ("blocked", None)
      - gated verdict cached → ("gated", None)
      - cached detail → ("ok", resp)
      - else one upstream ``content()`` fetch, with the block-russian
        check and the stateless group-key derivation applied before
        caching → ("ok", resp) or ("blocked", None)

    Upstream ProviderError propagates unchanged — the native route's
    ``upstream_guard`` (via ``content_provider_error``) converts a
    provider-raised ``gated`` into its client-visible 404, exactly as
    before. Health recording stays with the guard too.
    """
    cache_key = f"content:{provider_id}:{external_id}"
    if blocklist_cache.get(cache_key) is not None:
        return "blocked", None
    if gated_cache.get(cache_key) is True:
        return "gated", None
    cached = content_cache.get(cache_key)
    if cached is not None:
        return "ok", cast(ContentResponse, cached)
    resp = await PROVIDERS[provider_id].content(external_id, get_client())
    if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
        blocklist_cache.set(cache_key, True)
        return "blocked", None
    resp.group_key = group_key_from(resp.title, resp.form, resp.year, resp.id)
    content_cache.set(cache_key, resp)
    return "ok", resp


def is_hard_unavailable(group_key: str) -> bool:
    """True when a group key is DELIBERATELY unavailable — gated
    (subscription), blocklisted (Russian content), or unknown — as
    opposed to transiently unresolvable (an upstream blip).

    Ticket #224: the facade detail route uses this to decide between a
    degraded card answer (a known card whose live resolution failed
    should still render) and the D2 404 — a gated/blocked verdict must
    NEVER be masked by the degradation.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return True  # unknown group — the cold-cache 404 stands
    provider_id, item = next(iter(per_provider.items()))
    _, _, external_id = item.id.partition(":")
    cache_key = f"content:{provider_id}:{external_id}"
    if gated_cache.get(cache_key) is True:
        return True
    return blocklist_cache.get(cache_key) is not None


def should_skip_uakino_in_fanout() -> bool:
    """True when uakino must be dropped from a ``provider=all`` fan-out.

    Dropped while down (startup marker or sliding-window) or while the
    browser session has not yet become ready (issue #193). Shared by the
    native ``/api/search`` route and the facade search (ticket #106).
    """
    if "uakino" not in PROVIDERS:
        return False
    if TRACKER.status("uakino") == STATUS_DOWN:
        return True
    return not get_session().ready_event.is_set()


async def await_uakino_ready() -> None:
    """Gate an explicit uakino route on the browser session being ready.

    A deterministic startup marker short-circuits with 502 — the session
    is dead for the process lifetime, so waiting is pointless. Otherwise
    wait for ``ready_event`` bounded by ``WARM_WAIT_S``; a cold session
    still warming answers 503 ``warming`` so the client can back off and
    retry (issue #193/#196).
    """
    marker = TRACKER.startup_marker("uakino")
    if marker is not None:
        raise HTTPException(
            502,
            detail=ErrorResponse(
                error="upstream_unreachable", message=f"uakino {marker}"
            ).model_dump(),
        )
    session = get_session()
    try:
        await asyncio.wait_for(session.ready_event.wait(), timeout=WARM_WAIT_S)
    except TimeoutError:
        raise HTTPException(
            503,
            detail=ErrorResponse(
                error=STATUS_WARMING,
                message="uakino session is still warming; retry shortly",
            ).model_dump(),
        ) from None


async def merged_search(
    q: str,
    *,
    provider: str = "all",
    form: MediaForm | None = None,
    style_filter: frozenset[MediaStyle] | None = None,
) -> SearchResponse:
    """Multi-provider merged search with per-provider failure attribution
    (ADR-0002) — the shared core of BOTH the native ``/api/search`` route
    and the Jellyfin facade (ticket #106).

    Moved out of the route module (main.py) the same way ``load_home``
    was (ticket #101): one fan-out, one cache, one merge for every
    caller. Model B filter axes (ADR-0001, ticket #134): ``form`` is an
    exact-or-None match, ``style_filter`` a comma-list intersection
    (``None`` = any); both participate in the cache key so filtered and
    unfiltered searches never share an entry.

    Behaviour (unchanged from the route's contract):
      - 200 with ``failures`` populated whenever at least one provider's
        contribution failed; the field is omitted when no provider failed.
      - 502 ``search_timeout`` only when the overall budget expired for
        ALL providers; partial results on timeout return 200 with
        synthetic timeout rows.
      - ``provider=all`` skips uakino while its session is ``warming`` /
        pinned down (issue #193); explicit ``?provider=uakino`` bounded-
        waits on ``ready_event`` (502 on a startup marker, 503 warming on
        timeout — issue #196).
    """
    if not q.strip():
        # Defensive: the native route enforces min_length=1 at the FastAPI
        # boundary; the facade guards its own SearchTerm, so an empty query
        # never reaches the fan-out.
        return SearchResponse(query=q, groups=[])
    # Taste signal (spec #252): every search — from BOTH surfaces — feeds
    # «Рекомендовано для тебе». Deduped + bounded in the store, so a
    # repeat search from back-navigation just moves the query to the
    # front.
    record_search_query(q)
    # Fan-out skip (issue #193): while uakino's browser session is not
    # ready (warming) or pinned down, drop it from the ``provider=all``
    # fan-out instead of letting it burn the search budget on a session
    # that cannot serve. No failures entry — a cold session is not an
    # upstream error.
    skip_uakino = provider == "all" and should_skip_uakino_in_fanout()
    cache_key = f"search:{provider}:{q}:{form or ''}:{style_key(style_filter)}"
    if skip_uakino:
        # Distinguish "cold uakino" from "uakino returned empty" so a
        # warmed-up session never serves a stale uakino-less entry for the
        # same query (issue #193 cache obligation).
        cache_key += ":no-uakino"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cast(SearchResponse, cached)
    if provider == "uakino":
        # Explicit uakino: 502 on a startup marker, bounded wait on
        # ready_event, 503 ``warming`` on timeout (issue #196).
        await await_uakino_ready()
    if skip_uakino:
        selected = [p for p in PROVIDERS.values() if p.id != "uakino"]
    else:
        selected = list(PROVIDERS.values() if provider == "all" else [PROVIDERS[provider]])
    if not selected:
        # Every provider was dropped from the fan-out (e.g. uakino was the
        # only provider and it is cold): nothing to run — an empty response
        # is the honest answer, never a 502 (issue #193). Cached under the
        # ``:no-uakino`` key so it never shadows a warmed uakino result.
        resp = SearchResponse(query=q, groups=[])
        search_cache.set(cache_key, resp)
        return resp
    http = get_client()

    async def run(p: BaseProvider) -> list[SearchResult] | ProviderFailure:
        """Per-provider search that converts any exception into a ProviderFailure.

        Returns ``list[SearchResult]`` on success and ``ProviderFailure``
        on failure. A provider that returns ``[]`` with no exception is
        a legitimate "no match" answer and is NOT a failure (the empty
        list is the success signal). Health recording lives in the
        outer loop, not here, so partial-failure paths don't double-count.
        """
        try:
            return await p.search(q, http)
        except Exception as e:  # noqa: BLE001
            log.warning("search failed provider=%s err=%s", p.id, e)
            if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
                code = "timeout"
            else:
                code = "upstream_unreachable"
            return ProviderFailure(provider=p.id, code=code, message=str(e))

    # One task per provider, so the overall-timeout branch can observe
    # partial completion (ADR-0002 contract: "if it fires, any in-flight
    # providers that didn't complete get a synthetic timeout row").
    # `asyncio.wait` returns (done, pending) within the budget; we then
    # cancel pending and assemble the response — 502 only when no
    # provider completed at all.
    tasks: dict[asyncio.Task[list[SearchResult] | ProviderFailure], str] = {
        asyncio.create_task(run(p)): p.id for p in selected
    }
    done: set[asyncio.Task[list[SearchResult] | ProviderFailure]]
    pending: set[asyncio.Task[list[SearchResult] | ProviderFailure]]
    done, pending = await asyncio.wait(
        tasks.keys(),
        timeout=_config.SETTINGS.search_total_timeout_s,
    )

    # Cancel + drain the still-flying tasks. CancelledError is not
    # caught by `run()`'s `except Exception`, so a cancel leaves the
    # task in cancelled state; we don't iterate cancelled tasks below.
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=0.5)

    out_results: list[SearchResult] = []
    failures: list[ProviderFailure] = []

    # Drain done tasks into pid-keyed maps so we can iterate PROVIDERS in
    # registration order below. ``asyncio.wait`` returns done as a set,
    # which has nondeterministic iteration order — that propagates into
    # the response and breaks stable test assertions + UI source-order.
    # The PROVIDERS dict preserves insertion order (Python 3.7+), so we
    # use it as the canonical traversal key for results/failures too.
    results_by_pid: dict[str, list[SearchResult]] = {}
    failures_by_pid: dict[str, ProviderFailure] = {}
    for task in done:
        if task.cancelled():
            continue
        pid = tasks[task]
        try:
            content = task.result()
        except Exception as e:  # noqa: BLE001
            # Defensive: ``run()`` catches Exception everywhere; an
            # escapee is a programming error. Surface as an internal
            # failure attributed to the provider so the client sees a
            # structured signal rather than a partial response.
            log.warning("search unexpected escapee provider=%s err=%r", pid, e)
            TRACKER.record(pid, ok=False)
            failures_by_pid[pid] = ProviderFailure(
                provider=pid, code="internal", message=str(e)
            )
            continue
        if isinstance(content, ProviderFailure):
            TRACKER.record(pid, ok=False)
            failures_by_pid[pid] = content
        else:
            TRACKER.record(pid, ok=True)
            results_by_pid[pid] = content

    # Pending tasks: the overall budget fired before they completed.
    # Per ADR-0002, each one gets a synthetic ``timeout`` row.
    for task in pending:
        pid = tasks[task]
        failures_by_pid[pid] = ProviderFailure(
            provider=pid,
            code="timeout",
            message=f"overall budget {_config.SETTINGS.search_total_timeout_s}s exceeded",
        )

    # Subscription-gate sweep (can_gate providers): drop cards whose
    # only stream is the sponsor promo clip. Bounded so a slow sweep
    # degrades to keeping the cards instead of failing the search.
    for prov in PROVIDERS.values():
        if prov.can_gate and prov.id in results_by_pid:
            try:
                results_by_pid[prov.id] = await asyncio.wait_for(
                    filter_gated_items(results_by_pid[prov.id], http),
                    timeout=_GATE_CHECK_TIMEOUT_S,
                )
            except TimeoutError:
                pass

    # Emit results + failures in PROVIDERS registration order so the
    # response is deterministic regardless of which asyncio task
    # finishes first. The UI relies on stable source order for the
    # source-switching chip strip.
    for prov in PROVIDERS.values():
        pid = prov.id
        if pid in results_by_pid:
            out_results.extend(results_by_pid[pid])
        if pid in failures_by_pid:
            failures.append(failures_by_pid[pid])

    # Model B axis filter (ADR-0001, ticket #134): apply ``form`` /
    # ``style`` BEFORE the merge so a filtered search never forms a
    # group from a non-matching member (a merged group's canonical
    # ``form``/``styles`` come from its first source row).
    if form is not None or style_filter is not None:
        out_results = [
            r for r in out_results if matches_axes(r, form, style_filter)
        ]

    if not done and failures:
        # Every provider timed out — total failure is a server-side
        # problem, not a per-provider outcome. Surface as a clean error
        # (never cached per ADR-0003).
        log.warning(
            "search total-timeout exceeded q=%r providers=%d", q, len(selected)
        )
        raise HTTPException(
            502,
            detail=ErrorResponse(
                error="search_timeout",
                message=f"search exceeded {_config.SETTINGS.search_total_timeout_s}s for all {len(selected)} providers",
            ).model_dump(),
        ) from None

    # Build the response. Always cache 200 responses — including those
    # with populated failures (a flapping provider should not become a
    # permanent cache bypass per ADR-0003). The 502 path never reaches
    # this code because it raises above.
    #
    # v3 (issue #71): cross-provider duplicates are merged server-side
    # via ``merge_results`` (issue #52 / v3 spec §4). The result is a
    # ``groups: list[SearchGroup]`` payload — one entry per group_key,
    # each carrying the full per-provider ``sources`` list.
    groups = [
        SearchGroup(
            # Single projection (spec #309): canonical fields + member
            # keys from one place instead of re-deriving them here.
            group_key=proj.key,
            title=proj.title,
            year=proj.year,
            poster=proj.poster,
            form=proj.form,
            styles=proj.styles,
            genres=list(proj.genres),
            sources=list(proj.sources),
            member_keys=list(proj.member_keys),
        )
        for proj in (project_group(mg) for mg in merge_results(out_results))
    ]
    if failures:
        resp = SearchResponse(query=q, groups=groups, failures=failures)
    else:
        resp = SearchResponse(query=q, groups=groups)
    search_cache.set(cache_key, resp)
    return resp


def register_search_groups(groups: Sequence[SearchGroup]) -> None:
    """Fold search-result groups into the shared group-key resolution map.

    Ticket #106: the Jellyfin facade's search must open in the #105
    detail surface, and ``resolve_group_content`` only knows keys the
    resolution map (``sources_cache``) carries. A search covers the whole
    catalog — most results are NOT in the 30-min home snapshot — so the
    facade registers each merged group's provider union under EVERY
    member key it holds, the same shape ``_build_sources_map`` stores for
    home rows. First-seen provider order is preserved; providers the map
    already knows are left untouched. The whole map keeps the home
    cache's TTL (ADR-0003), so a registered key expires with the next
    snapshot refresh.
    """
    if not groups:
        return
    existing: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    changed = False
    for g in groups:
        union = provider_union(g.sources)
        keys = g.member_keys or [g.group_key]
        for key in keys:
            current = existing.get(key)
            if current is None:
                existing[key] = dict(union)
                changed = True
            else:
                merged = dict(current)
                for pid, item in union.items():
                    merged.setdefault(pid, item)
                if merged != current:
                    existing[key] = merged
                    changed = True
    if changed:
        # Re-set refreshes the TTL on the whole map (ADR-0003): a search
        # extends the snapshot's life, never shortens it.
        sources_cache.set(_SOURCES_KEY, existing)


__all__ = [
    "WARM_WAIT_S",
    "await_uakino_ready",
    "blocklist_cache",
    "content_cache",
    "deep_page_cache",
    "extend_row_pool",
    "filter_gated_items",
    "gated_cache",
    "get_home",
    "home_cache",
    "load_home",
    "merged_search",
    "register_search_groups",
    "resolve_group",
    "resolve_group_content",
    "row_deep_cache",
    "search_cache",
    "should_skip_uakino_in_fanout",
    "sources_cache",
]
