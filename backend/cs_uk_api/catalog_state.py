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

from . import config as _config
from .cache import TtlCache
from .health import TRACKER
from .home import build_home_rows
from .http_client import get_client
from .merge import item_group_key
from .models import HomeResponse, SearchResult
from .providers import PROVIDERS

log = logging.getLogger("cs_uk_api.catalog_state")

#: v3 (issue #70): the merged home view — «Новинки» + «Популярні зараз»
#: + the five type rows — is a curated snapshot, refreshed every 30 min.
home_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_home_s)

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


__all__ = [
    "get_home",
    "home_cache",
    "load_home",
    "resolve_group",
    "sources_cache",
]
