"""Multi-provider merged search fan-out (spec #309 T5).

The search half of the catalog state: ``merged_search`` — the shared
core of BOTH the native ``/api/search`` route and the Jellyfin facade
(ticket #106). One fan-out, one cache, one merge for every caller,
with per-provider failure attribution (ADR-0002) and the subscription-
gate sweep folded in.

Depends on ``_stores`` (the search cache + the search-query taste
signal) and ``resolution`` (gating, uakino readiness, group-key
machinery). Never imported by the other internal modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import httpx
from fastapi import HTTPException

from .. import config as _config
from ..filters import matches_axes, style_key
from ..health import TRACKER
from ..http_client import get_client
from ..merge import merge_results
from ..models import (
    ErrorResponse,
    MediaForm,
    MediaStyle,
    ProviderFailure,
    SearchGroup,
    SearchResponse,
    SearchResult,
)
from ..providers import PROVIDERS
from ..providers.base import BaseProvider
from ..wire_identity import project_group
from ._stores import record_search_query, search_cache
from .resolution import (
    GATE_CHECK_TIMEOUT_S,
    await_uakino_ready,
    filter_gated_items,
    should_skip_uakino_in_fanout,
)

log = logging.getLogger("cs_uk_api.catalog_state.search")


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
                    timeout=GATE_CHECK_TIMEOUT_S,
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
