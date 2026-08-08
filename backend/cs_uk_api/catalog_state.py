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

from . import config as _config
from .cache import TtlCache
from .country import is_blocked_country
from .health import TRACKER
from .home import build_home_rows
from .http_client import get_client
from .merge import group_key_from, item_group_key
from .models import ContentResponse, HomeResponse, SearchResult
from .providers import PROVIDERS
from .providers.base import ProviderError

log = logging.getLogger("cs_uk_api.catalog_state")

#: v3 (issue #70): the merged home view — «Новинки» + «Популярні зараз»
#: + the five type rows — is a curated snapshot, refreshed every 30 min.
home_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_home_s)

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
#: ``/Items/{g1:...}`` resolves provider+external from it. ``g1:`` ids
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
    """
    out: dict[str, dict[str, SearchResult]] = {}
    for source_map in (newest, popular):
        for items in source_map.values():
            for it in items:
                _add_listing_to_sources_map(out, it)
    for source_map in by_type.values():
        for items in source_map.values():
            for it in items:
                _add_listing_to_sources_map(out, it)
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
            if section.type in {"movie", "series", "anime", "cartoon", "dorama"}:
                tasks.append(asyncio.create_task(_type_section(pid, section.id, section.type)))

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
        mapping[pid] = await filter_gated_items(mapping[pid], http)

    # Only can_gate providers need the sweep (the filter is a no-op for
    # everyone else) — and only when their listing is non-empty.
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
        try:
            _done, pending = await asyncio.wait(sweep, timeout=_GATE_CHECK_TIMEOUT_S)
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)

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
#: never plays) rather than failing the whole home build.
_GATE_CHECK_CONCURRENCY = 24
_GATE_CHECK_TIMEOUT_S = 25.0


def _gate_cache_key(item: SearchResult) -> str:
    """The shared ``content:{provider}:{external}`` key for a card."""
    _, _, external = item.id.partition(":")
    return f"content:{item.provider}:{external}"


async def filter_gated_items(
    items: Sequence[SearchResult], http: httpx.AsyncClient
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
    todo = [
        it
        for it in items
        if it.provider in PROVIDERS and PROVIDERS[it.provider].can_gate
    ]
    if not todo:
        return list(items)
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
            resp.group_key = group_key_from(resp.title, resp.type, resp.year, resp.id)
            content_cache.set(key, resp)
            # A known-good verdict follows the CONTENT TTL (not the long
            # gated TTL): an un-gated title is re-checked when its
            # content cache expires, so it re-enters the catalog as soon
            # as the upstream publishes the real video.
            gated_cache.set(key, False, ttl_s=_config.SETTINGS.cache_content_s)
            return False

    tasks = {asyncio.create_task(check(it)): it for it in todo}
    gated_ids: set[str] = set()
    try:
        done, _pending = await asyncio.wait(tasks.keys(), timeout=_GATE_CHECK_TIMEOUT_S)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.wait(tasks.keys(), timeout=1)
    for t in done:
        item = tasks[t]
        try:
            if t.result():
                gated_ids.add(item.id)
        except Exception:  # noqa: BLE001, S110
            pass
    return [it for it in items if it.id not in gated_ids]


def resolve_group(group_key: str) -> dict[str, SearchResult] | None:
    """Resolution for a ``g1:`` group key (ticket #101).

    Returns the ``provider -> SearchResult`` map for the group, or
    ``None`` when the key is absent (cold cache → the caller yields a
    404 "item unavailable", which Jellyfin clients tolerate).
    """
    per_provider: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    return per_provider.get(group_key)


async def resolve_group_content(group_key: str) -> ContentResponse | None:
    """Resolve a ``g1:`` group key to ONE provider's content detail.

    The facade's ticket #105 detail path: a ``g1:`` key maps to the same
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
    try:
        resp = await provider.content(external_id, http)
        TRACKER.record(provider_id, ok=True)
    except Exception as e:  # noqa: BLE001
        log.warning("group content failed provider=%s key=%s err=%s", provider_id, group_key, e)
        TRACKER.record(provider_id, ok=False)
    if resp is None:
        return None
    if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
        blocklist_cache.set(cache_key, True)
        log.info("blocked Russian content id=%s country=%s", cache_key, resp.country)
        return None
    resp.group_key = group_key
    content_cache.set(cache_key, resp)
    return resp


__all__ = [
    "blocklist_cache",
    "content_cache",
    "filter_gated_items",
    "gated_cache",
    "get_home",
    "home_cache",
    "load_home",
    "resolve_group",
    "resolve_group_content",
    "sources_cache",
]
