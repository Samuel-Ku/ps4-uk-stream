"""Home snapshot build + deep rows (spec #309 T5).

The snapshot half of the catalog state: the full provider fan-out →
merged rows → cache + persist path (``load_home`` / ``_build_home`` /
``_cache_home``), the group-key resolution map built from the raw
listings (``_build_sources_map``), and the lazy deep-row extension
(``extend_row_pool``, spec #305). The single load path both the native
``/api/home`` route and the Jellyfin facade use.

Depends on ``_stores`` (caches, playback entries, snapshot store),
``resolution`` (group keys + the gate sweep) and ``warm`` (the
personalized rows + background profile warm). Never imported by the
other internal modules — the package dependency DAG ends here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import cast

from .. import config as _config
from ..health import TRACKER, record_verdict
from ..home import build_home_rows, round_robin_dedup, section_row_type
from ..http_client import get_client
from ..merge import item_group_key, merge_results
from ..models import HomeItem, HomeResponse, SearchResult
from ..providers import PROVIDERS
from ..providers.base import ProviderError
from ..row_kinds import ROW_KINDS
from ..wire_identity import project_group, provider_union
from ._stores import (
    _HOME_KEY,
    _SOURCES_KEY,
    GroupIndexEntry,
    _set_group_index,
    _snapshot_store,
    deep_page_cache,
    home_cache,
    playback_entries,
    recent_history_entries,
    row_deep_cache,
    sources_cache,
)
from .resolution import (
    _GATE_CHECK_CONCURRENCY,
    GATE_CHECK_TIMEOUT_S,
    episode_group_key,
    filter_gated_items,
)
from .warm import _warm_profiles, _with_recommendation_rows

log = logging.getLogger("cs_uk_api.catalog_state.snapshot")


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
        # the persisted rows works without any provider call. The index
        # beside sources_cache is repopulated from the persisted rows so
        # the seam answers instantly too (spec #364).
        home_cache.set(_HOME_KEY, persisted)
        if sources is not None:
            sources_cache.set(_SOURCES_KEY, sources)
        _populate_group_index(persisted)
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
            record_verdict(pid, e.code if isinstance(e, ProviderError) else None)
            return
        TRACKER.record(pid, ok=True)
        if results:
            newest_lists[pid] = list(results)

    async def _popular(pid: str, section_id: str) -> None:
        try:
            results, _ = await PROVIDERS[pid].browse(section_id, 1, http)
        except Exception as e:  # noqa: BLE001
            log.warning("home popular skipped provider=%s err=%s", pid, e)
            record_verdict(pid, e.code if isinstance(e, ProviderError) else None)
            return
        TRACKER.record(pid, ok=True)
        if results:
            popular_lists[pid] = list(results)

    async def _type_section(pid: str, section_id: str, type_key: str) -> None:
        try:
            results, _ = await PROVIDERS[pid].browse(section_id, 1, http)
        except Exception as e:  # noqa: BLE001
            log.warning("home type skipped provider=%s section=%s err=%s", pid, section_id, e)
            record_verdict(pid, e.code if isinstance(e, ProviderError) else None)
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
        _done, pending = await asyncio.wait(sweep, timeout=GATE_CHECK_TIMEOUT_S)
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
    # Index beside sources_cache: built at _cache_home alongside the
    # sources map; repopulated on persisted cold start; merged
    # incrementally in register_search_groups — single mutation site
    # so map and index cannot diverge.
    _populate_group_index(resp)
    _snapshot_store().save(resp, sources)
    return resp


def _populate_group_index(home: HomeResponse) -> None:
    """(Re)build the group index from a HomeResponse's rows."""
    entries: dict[str, GroupIndexEntry] = {}
    for row in home.rows:
        for it in row.items:
            keys = it.member_keys or [it.group_key]
            for k in keys:
                if k not in entries:
                    entries[k] = GroupIndexEntry(home_item=it, row_type=row.type)
    _set_group_index(entries)


def get_home() -> HomeResponse | None:
    """Cached home snapshot without triggering a build (None on cold cache)."""
    return cast(HomeResponse, home_cache.get(_HOME_KEY))


# ---------------------------------------------------------------------------
# Deep rows (spec #305): lazy pagination of home rows
# ---------------------------------------------------------------------------

#: Form filter for the form-split recent rows: a movie-form recent row
#: must never pick up series cards from a provider's deeper newest
#: pages (mirrors the page-1 build's per-form filter).
_RECENT_ROW_FORM = {"recent_movie": "movie", "recent_series": "series"}


def _row_is_extendable(row_type: str) -> bool:
    """Does the row kind page beyond the snapshot? (spec #305, #362 C)

    Reads the row-kind table's ``extendable`` flag: the type rows, the
    form-split «Нещодавно додані» rows and «Популярні зараз» page; the
    personalized rows («Нові серії», «Нещодавно переглянуто»), the LLM
    idea slots and any non-table kind (the ``genre:<slug>`` rails, the
    recipe-inserted personalized rows) are bounded — unknown kinds
    answer False instead of raising.
    """
    entry = ROW_KINDS.get(row_type)
    return entry is not None and entry.extendable


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
    if not _row_is_extendable(row_type) or not snapshot_items:
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
                record_verdict(pid, e.code if isinstance(e, ProviderError) else None)
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
