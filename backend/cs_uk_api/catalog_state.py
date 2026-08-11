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
from typing import cast

import httpx
from fastapi import HTTPException

from . import config as _config
from .cache import TtlCache
from .country import is_blocked_country
from .filters import matches_axes, style_key
from .health import TRACKER
from .home import build_home_rows, section_row_type
from .http_client import get_client
from .merge import group_key_from, item_group_key, merge_results
from .models import (
    STATUS_DOWN,
    STATUS_WARMING,
    ContentResponse,
    ErrorResponse,
    HomeResponse,
    MediaForm,
    MediaStyle,
    ProviderFailure,
    SearchGroup,
    SearchResponse,
    SearchResult,
)
from .providers import PROVIDERS
from .providers.base import BaseProvider, ProviderError
from .uakino_browser import get_session

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
        union: dict[str, SearchResult] = {}
        for s in mg.sources:
            union.setdefault(s.provider, s)
        member_keys = list(dict.fromkeys(item_group_key(s) for s in mg.sources))
        for key in member_keys:
            # Replace (not setdefault): the union is built from the same
            # first-seen walk order, so this keeps the exact provider
            # order the home row surfaces.
            out[key] = union
    return out


async def load_home() -> HomeResponse:
    """Return the merged home snapshot, building it on a cache miss.

    This is the single load path for BOTH the native ``/api/home`` route
    and the Jellyfin facade. On a hit no provider is re-invoked; on a
    miss the full provider fan-out runs under the shared search budget
    (same behaviour the native route documented).
    """
    cached = home_cache.get(_HOME_KEY)
    if cached is not None:
        return cast(HomeResponse, cached)

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
            provider = PROVIDERS.get(pid)
            if items and provider is not None and provider.can_gate:
                sweep.append(asyncio.create_task(_sweep(mapping, pid)))
    for per_pid in type_lists.values():
        for pid, items in list(per_pid.items()):
            provider = PROVIDERS.get(pid)
            if items and provider is not None and provider.can_gate:
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
                rows = build_home_rows(
                    newest=newest_lists,
                    popular=popular_lists,
                    by_type=type_lists,
                    newest_limit=_config.SETTINGS.home_row_limit,
                )
                resp = HomeResponse(rows=rows)
                home_cache.set(_HOME_KEY, resp)
                sources_cache.set(
                    _SOURCES_KEY,
                    _build_sources_map(newest_lists, popular_lists, type_lists),
                )

            asyncio.create_task(_finish_sweep(pending))

    rows = build_home_rows(
        newest=newest_lists,
        popular=popular_lists,
        by_type=type_lists,
        newest_limit=_config.SETTINGS.home_row_limit,
    )
    resp = HomeResponse(rows=rows)
    home_cache.set(_HOME_KEY, resp)
    sources_cache.set(_SOURCES_KEY, _build_sources_map(newest_lists, popular_lists, type_lists))
    return resp


def get_home() -> HomeResponse | None:
    """Cached home snapshot without triggering a build (None on cold cache)."""
    return cast(HomeResponse, home_cache.get(_HOME_KEY))


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
    """
    per_provider: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    for group_key, providers in per_provider.items():
        for result in providers.values():
            if result.id == composite:
                return group_key
    return None


#: Per-item playback positions reported by the client (ticket #214):
#: ``item_id -> position_ticks``. The facade has a single fixed user (D4),
#: so no per-user dimension. In-memory only (ADR-0003: no persistence).
_playback: dict[str, int] = {}


def record_playback(item_id: str, position_ticks: int) -> None:
    """Record the client's playback position (``Sessions/Playing/*``).

    Last report wins — the client streams Progress heartbeats while
    playing and a final Stopped report, so the newest position is the
    most accurate. Zero/negative positions (a just-started item) are
    ignored; a later positive report overwrites.
    """
    if position_ticks <= 0:
        return
    _playback[item_id] = position_ticks


def playback_positions() -> dict[str, int]:
    """item_id -> position_ticks, most-progressed first (ticket #214)."""
    return dict(sorted(_playback.items(), key=lambda kv: kv[1], reverse=True))


def clear_playback() -> None:
    """Drop all recorded positions (test isolation, #214)."""
    _playback.clear()


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
                # ADR-0002: a gated verdict never moves the health tracker.
                # Cache it so every later group-key call short-circuits —
                # the load_home sweep normally populates `gated_cache`
                # first, but a cold-cache g2: detail call must not record a
                # health-down here either (#139).
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
            group_key=mg.key,
            title=mg.sources[0].title,
            year=mg.sources[0].year,
            poster=mg.sources[0].poster,
            # Model B (contract #135): first-seen-wins, like the other
            # canonical fields.
            form=mg.sources[0].form,
            styles=mg.sources[0].styles,
            genres=list(mg.sources[0].genres),
            sources=list(mg.sources),
            # Issue #89: every per-item group key that contributed to
            # this merged card. Deduped, first-seen order. The canonical
            # ``group_key`` is the yearful-preferred-min; the client
            # matches a resume entry against ANY member key, not only
            # ``group_key``.
            member_keys=list(dict.fromkeys(item_group_key(s) for s in mg.sources)),
        )
        for mg in merge_results(out_results)
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
        union: dict[str, SearchResult] = {}
        for s in g.sources:
            union.setdefault(s.provider, s)
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
    "filter_gated_items",
    "gated_cache",
    "get_home",
    "home_cache",
    "load_home",
    "merged_search",
    "register_search_groups",
    "resolve_group",
    "resolve_group_content",
    "search_cache",
    "should_skip_uakino_in_fanout",
    "sources_cache",
]
