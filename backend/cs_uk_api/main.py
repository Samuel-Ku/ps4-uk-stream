from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar, cast

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

import cs_uk_api.providers._registry  # noqa: F401

from .cache import TtlCache
from .catalog_state import _GATE_CHECK_TIMEOUT_S, resolve_group
from .catalog_state import blocklist_cache as _catalog_blocklist_cache
from .catalog_state import content_cache as _catalog_content_cache
from .catalog_state import filter_gated_items as _filter_gated_items
from .catalog_state import gated_cache as _catalog_gated_cache
from .catalog_state import get_home as _catalog_get_home
from .catalog_state import home_cache as _catalog_home_cache
from .catalog_state import load_home as _catalog_load_home
from .catalog_state import sources_cache as _catalog_sources_cache
from .config import SETTINGS
from .country import is_blocked_country
from .health import TRACKER
from .http_client import close_client, get_client
from .jellyfin import router as jellyfin_router
from .jellyfin.capture import capture_request
from .jellyfin.router import normalize_jellyfin_path
from .merge import group_key_from, item_group_key, merge_results
from .models import (
    BrowseResponse,
    ContentResponse,
    ErrorResponse,
    GroupContentResponse,
    GroupSourceContentResponse,
    HomeResponse,
    MediaForm,
    MediaStyle,
    ProviderFailure,
    ProviderInfo,
    ProviderSections,
    SearchGroup,
    SearchResponse,
    SearchResult,
    Section,
    StreamResponse,
)
from .poster_proxy import fetch as fetch_poster
from .providers import PROVIDERS
from .providers.base import BaseProvider, ProviderError
from .uakino_browser import DEFAULT_CHROMIUM, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cs_uk_api")

#: Valid ``?style=`` tokens (Model B, ticket #134). Kept as a module
#: constant because ``MediaStyle.__args__`` is a typing special form
#: that mypy rejects on attribute access.
_STYLE_TOKENS: frozenset[str] = frozenset({"anime", "cartoon", "dorama"})

# uakino's browser-session provider cannot work without a system Chromium
# binary (v3 spec §2.1): mark it down deterministically at startup instead
# of letting it fail per-request.
if not os.path.exists(DEFAULT_CHROMIUM):
    TRACKER.mark_startup("uakino", "chromium_missing")
    log.warning("uakino marked down at startup: chromium binary not found at %s", DEFAULT_CHROMIUM)

_search_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)
_content_cache = _catalog_content_cache
_browse_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)
_blocklist_cache = _catalog_blocklist_cache

#: Back-compat aliases (tests import these): the home snapshot + group-key
#: resolution map now live in ``cs_uk_api.catalog_state`` (ticket #101),
#: shared by the native ``/api/*`` routes and the Jellyfin facade.
#: Clearing them clears the shared state.
_home_cache = _catalog_home_cache
_home_sources_cache = _catalog_sources_cache

T = TypeVar("T")

# Sentinel for ``_upstream_guard(..., on_error=...)``: distinguishes the
# parameter's "no default provided" state from a legitimate ``None`` default.
# Callers that want to degrade to None on upstream failure must pass
# ``on_error=None`` explicitly; omitting the kwarg means "raise 502".
_UNSET: object = object()


def _split_content_id(content_id: str) -> tuple[str, str]:
    """Content id "provider:external" -> (provider, external).

    The named accessor for the provider-by-prefix derivation shared by the
    content and stream routes; malformed ids yield ("", "").
    """
    provider_id, _, external_id = content_id.partition(":")
    return provider_id, external_id


async def _upstream_guard(
    provider_id: str,
    coro: Awaitable[T],
    log_label: str,
    *,
    on_error: T | object = _UNSET,
    exc_handler: Callable[[Exception], None] | None = None,
) -> T:
    """Await an upstream provider call with health recording + the 502 guard.

    The record+log+raise(502) pattern shared by every upstream try/except
    site lives here and nowhere else. The failure path runs in this order:

      1. ``exc_handler(e)`` — if provided, runs first. It either raises
         (translating the upstream error into a client-side response such
         as 400/404) or returns. The helper does NOT record when the
         handler raises; translation-level errors are not upstream-health
         signals.
      2. ``TRACKER.record(provider_id, ok=False)`` + warning log.
      3. Either return ``on_error`` (when an explicit default was passed)
         or raise the canonical 502 ``upstream_unreachable``.

    ``on_error`` uses the ``_UNSET`` sentinel (NOT ``None``) as its
    default, so callers can distinguish "no default" (raises 502) from
    "degrade to None" (returns None). Pass ``on_error=None`` explicitly
    when the degraded value legitimately is None.
    """
    try:
        result = await coro
    except Exception as e:
        if exc_handler is not None:
            exc_handler(e)
        TRACKER.record(provider_id, ok=False)
        log.warning("%s failed provider=%s err=%s", log_label, provider_id, e)
        if on_error is _UNSET:
            raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
        # The sentinel check above guarantees this is a real T (the caller
        # passed an explicit default), but mypy cannot narrow ``T | object``
        # to ``T`` from ``is not _UNSET`` alone.
        return cast(T, on_error)
    TRACKER.record(provider_id, ok=True)
    return result


def _content_provider_error(e: Exception) -> None:
    """Subscription-gated content is a client-visible 404, not an
    upstream-health signal — the item is deliberately unavailable."""
    if isinstance(e, ProviderError) and e.code == "gated":
        raise HTTPException(404, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e


def _stream_provider_error(e: Exception) -> None:
    """Translation-level validation errors are client-side semantics, not
    upstream-health signals — they must not move the needle. A gated
    stream is a deliberate "no playable file" verdict → 404."""
    if not isinstance(e, ProviderError):
        return
    if e.code == "invalid_translation":
        raise HTTPException(400, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e
    if e.code in ("translation_missing", "gated"):
        raise HTTPException(404, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # The uakino browser session is lazily created on first request and
    # runs a headless Chromium; close it on shutdown so SIGTERM doesn't
    # orphan the browser process. `close()` is a no-op when the session
    # was never started.
    await get_session().close()
    await close_client()


app = FastAPI(title="cs-uk-api", version="0.1.0", lifespan=lifespan)

# Jellyfin facade (spec #100): mounted at the Jellyfin paths, deliberately
# NOT under /api/* — the native contract is untouched and a Jellyfin
# client pointed at host:port finds a server without configuration.
app.include_router(jellyfin_router)


@app.middleware("http")
async def jellyfin_case_normalize(request: Request, call_next):
    """Rewrite Jellyfin facade paths to canonical case.

    Real Jellyfin routes case-insensitively; FastAPI does not. A client
    like Switchfin sends ``/Users/authenticatebyname`` where the facade
    declares ``POST /Users/AuthenticateByName`` — without this rewrite
    that request 404s. Non-facade paths are untouched.
    """
    canonical = normalize_jellyfin_path(request.url.path)
    if canonical is not None:
        request.scope["path"] = canonical
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    response: Response = await call_next(request)
    latency_ms = int((time.monotonic() - started) * 1000)
    log.info("%s %s -> %s (%dms)", request.method, request.url.path, response.status_code, latency_ms)
    # Capture-first (ticket #103): record facade request sequences for
    # fixture freezing. No-op unless CS_UK_JF_CAPTURE_DIR is set.
    capture_request(request, response)
    return response


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(error="internal", message=str(exc)).model_dump(),
    )


@app.get("/api/providers")
async def list_providers() -> list[ProviderInfo]:
    return [
        ProviderInfo(
            id=p.id,
            name=p.name,
            types=list(p.types),  # type: ignore[arg-type]
            status=TRACKER.status(p.id),
            last_error_at=TRACKER.last_error_at(p.id),
        )
        for p in PROVIDERS.values()
    ]


@app.get("/api/sections")
async def list_sections() -> list[ProviderSections]:
    """Return only providers that opt into section browsing."""
    return [
        ProviderSections(provider=p.id, name=p.name, sections=list(p.sections))
        for p in PROVIDERS.values()
        if p.sections
    ]


@app.get("/api/browse")
async def browse(
    provider: str = Query(...),
    section: str = Query(...),
    page: int = Query(1, ge=1),
) -> BrowseResponse:
    if provider not in PROVIDERS:
        raise HTTPException(400, detail=ErrorResponse(error="unknown_provider", message=provider).model_dump())
    p = PROVIDERS[provider]
    if not p.sections:
        raise HTTPException(400, detail=ErrorResponse(error="not_browsable", message=provider).model_dump())
    if not p.has_section(section):
        raise HTTPException(404, detail=ErrorResponse(error="unknown_section", message=section).model_dump())
    cache_key = f"browse:{provider}:{section}:{page}"
    cached = _browse_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    results, has_next = await _upstream_guard(
        provider,
        p.browse(section, page, get_client()),
        f"browse section={section} page={page}",
    )
    if p.can_gate:
        # Subscription-gate sweep: drop cards whose only stream is the
        # sponsor promo clip before they surface in the listing.
        try:
            results = await asyncio.wait_for(
                _filter_gated_items(results, get_client()),
                timeout=_GATE_CHECK_TIMEOUT_S,
            )
        except TimeoutError:
            pass  # keep the cards; stream() still refuses gated items
    # Model B section filter (ADR-0001, ticket #134): the section's
    # ``form``/``styles`` axes narrow its own browse results (CONTEXT.md
    # «Section schema» match semantics — 3-case styles). Sections that
    # haven't declared axes (both ``None``) pass everything, so this is
    # a no-op for today's un-migrated sections.
    section_def = next(s for s in p.sections if s.id == section)
    if section_def.form is not None or section_def.styles is not None:
        results = [r for r in results if _section_matches(r, section_def)]
    resp = BrowseResponse(provider=provider, section=section, page=page, has_next=has_next, results=results)
    _browse_cache.set(cache_key, resp)
    return resp


def _section_matches(item: SearchResult, section: Section) -> bool:
    """Model B section match semantics (CONTEXT.md «Section schema»).

    - ``form``: ``None`` passes everything; else ``item.form ==
      section.form`` must hold.
    - ``styles``: 3-case — ``None`` passes anything (including empty);
      ``frozenset()`` (∅) passes only ordinary-only items
      (``item.styles == frozenset()``); a non-empty set passes iff
      ``item.styles & section.styles`` is non-empty (intersection).
    """
    if section.form is not None and item.form != section.form:
        return False
    if section.styles is None:
        return True
    if not section.styles:
        return not item.styles
    return bool(item.styles & section.styles)


def _parse_style_filter(raw: str | None) -> frozenset[MediaStyle] | None:
    """Parse the ``?style=`` query param into a style filter set.

    Decided semantics (CONTEXT.md «Search filter axes»): a comma-
    separated list with intersection matching — an item passes iff it
    carries at least one of the requested styles. Absent/empty = any
    (``None``). There is deliberately NO ordinary-only token on search:
    ``Section`` is the way to filter to ordinary-only (∅ styles), and
    ``?style`` stays a plain intersection list.

    Invalid style tokens raise a 400 — a typo should surface at the
    API boundary, not silently pass everything.
    """
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    invalid = [p for p in parts if p not in _STYLE_TOKENS]
    if invalid:
        raise HTTPException(
            400,
            detail=ErrorResponse(
                error="invalid_style",
                message=f"unknown style token(s): {', '.join(invalid)}",
            ).model_dump(),
        )
    return frozenset(cast(MediaStyle, p) for p in parts)


def _style_key(style_filter: frozenset[MediaStyle] | None) -> str:
    """Stable cache-key fragment for a style filter."""
    if not style_filter:
        return ""
    return ",".join(sorted(style_filter))


def _matches_axes(
    item: SearchResult,
    form: MediaForm | None,
    style_filter: frozenset[MediaStyle] | None,
) -> bool:
    """Model B axis match for a single search result (ADR-0001).

    - ``form``: exact-or-None — ``None`` passes, else
      ``item.form == form`` must hold. An item whose provider hasn't
      populated ``form`` yet (``None``) fails an explicit filter.
    - ``style_filter``: ``None`` passes; a non-empty set passes iff
      ``item.styles & style_filter`` is non-empty (intersection).
    """
    return (form is None or item.form == form) and (
        style_filter is None or bool(item.styles & style_filter)
    )


@app.get("/api/search", response_model=SearchResponse, response_model_exclude_unset=True)
async def search(
    q: str = Query(min_length=1, max_length=80),
    provider: str = Query("all"),
    form: MediaForm | None = Query(default=None),  # noqa: B008
    style: str | None = Query(default=None),
) -> SearchResponse:
    """Multi-provider search with per-provider failure attribution (ADR-0002).

    Note: ``form`` carries a ``# noqa: B008`` — ruff's B008 immutable-
    annotation check doesn't resolve the ``MediaForm`` type alias, so it
    false-positives on that line only (the identical ``str``-typed
    ``source`` param below passes clean).

    Model B filter axes (ADR-0001, ticket #134):
      - ``form=movie|series`` — exact-or-None; absent = any.
      - ``style=anime|cartoon|dorama[,anime,...]`` — comma-separated
        list, intersection semantics (an item passes iff it carries at
        least one requested style); absent = any. No ordinary-only
        token on search — that filter lives on Section (CONTEXT.md).
    Both axes participate in the cache key, so filtered and unfiltered
    searches for the same ``q`` never share an entry (ADR-0001
    obligation, fulfilled here).

    Behaviour:
      - 200 OK with ``failures: list[ProviderFailure]`` whenever at least
        one provider's contribution failed; the failures field is omitted
        from the JSON when no provider failed (``exclude_unset`` semantics).
      - 502 with ``ErrorResponse(error="search_timeout", ...)`` only when
        the overall 12s budget expired for ALL providers — i.e. nothing
        usable came back in time. Partial results on timeout return 200
        with synthetic timeout rows; total-failure returns 502.
    """
    if provider != "all" and provider not in PROVIDERS:
        raise HTTPException(400, detail=ErrorResponse(error="unknown_provider", message=provider).model_dump())
    style_filter = _parse_style_filter(style)
    cache_key = f"search:{provider}:{q}:{form or ''}:{_style_key(style_filter)}"
    cached = _search_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    selected = list(PROVIDERS.values() if provider == "all" else [PROVIDERS[provider]])
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
        timeout=SETTINGS.search_total_timeout_s,
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
            message=f"overall budget {SETTINGS.search_total_timeout_s}s exceeded",
        )

    # Subscription-gate sweep (can_gate providers): drop cards whose
    # only stream is the sponsor promo clip. Bounded so a slow sweep
    # degrades to keeping the cards instead of failing the search.
    for prov in PROVIDERS.values():
        if prov.can_gate and prov.id in results_by_pid:
            try:
                results_by_pid[prov.id] = await asyncio.wait_for(
                    _filter_gated_items(results_by_pid[prov.id], http),
                    timeout=_GATE_CHECK_TIMEOUT_S,
                )
            except TimeoutError:
                pass

    # Emit results + failures in PROVIDERS registration order so the
    # response is deterministic regardless of which asyncio task
    # finishes first. The UI relies on stable source order for the
    # source-switching chip strip. Use ``prov`` to avoid shadowing the
    # function's ``provider`` query parameter (which is typed as ``str``).
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
            r for r in out_results if _matches_axes(r, form, style_filter)
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
                message=f"search exceeded {SETTINGS.search_total_timeout_s}s for all {len(selected)} providers",
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
    # each carrying the full per-provider ``sources`` list. The UI
    # renders one card per group; opening it hits
    # ``/api/content/{group_key}`` (issue #70) which then loads the
    # merged detail with the same ``g1:…`` key.
    groups = [
        SearchGroup(
            group_key=mg.key,
            title=mg.sources[0].title,
            year=mg.sources[0].year,
            type=mg.sources[0].type,
            poster=mg.sources[0].poster,
            # Model B (issue #129): first-seen-wins, like the other
            # canonical fields.
            form=mg.sources[0].form,
            styles=mg.sources[0].styles,
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
    _search_cache.set(cache_key, resp)
    return resp


@app.get("/api/home", response_model=HomeResponse)
async def home() -> HomeResponse:
    """Merged home view (issue #70).

    Composition:

      - «Новинки» — round-robin across providers that opt into
        ``newest_section`` (animeon, animeua, anitubeinua, unimay,
        simpsonsuatv at time of writing), deduped by groupKey, capped
        at 20.
      - «Популярні зараз» — only when animeon's ``popular`` section
        returns at least one item (spec AC: present iff animeon
        provides it).
      - Five type rows (movie, series, anime, cartoon, dorama) — each
        aggregates every provider section whose ``Section.type``
        matches. Empty types are omitted.

    Cached for ``SETTINGS.cache_home_s`` (30 minutes by default). On a
    cache hit the providers are not re-invoked.

    Shared with the Jellyfin facade since ticket #101: the build runs in
    ``catalog_state.load_home`` so the facade resolves the same snapshot.
    """
    return await _catalog_load_home()


@app.get("/api/content/{content_id:path}")
async def content(
    content_id: str,
    source: str | None = Query(default=None),
) -> ContentResponse | GroupContentResponse | GroupSourceContentResponse:
    """Discriminator: ``g1:…`` group keys route to the merged lookup;
    everything else is the existing ``provider:external`` content path.

    For ``g1:…`` keys, an optional ``?source=<provider>`` query param
    routes to the lazy single-source fetch (issue #60 / v3 spec §3.3):
    returns that ONE source's v2 ContentResponse + a ``providers`` echo
    for the source-switching chip strip. Without ``?source=``, the
    legacy ``GroupContentResponse{item, providers}`` shape is returned
    (preserved for backwards compatibility).
    """
    if content_id.startswith("g1:"):
        if source is not None:
            return await _content_by_group_key_and_source(content_id, source)
        return await _content_by_group_key(content_id)
    return await _content_by_id(content_id)


async def _content_by_id(content_id: str) -> ContentResponse:
    cache_key = f"content:{content_id}"
    if _blocklist_cache.get(cache_key) is not None:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    if _catalog_gated_cache.get(cache_key) is True:
        raise HTTPException(404, detail=ErrorResponse(error="gated", message=content_id).model_dump())
    cached = _content_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    provider_id, external_id = _split_content_id(content_id)
    if provider_id not in PROVIDERS or not external_id:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    http = get_client()
    resp = await _upstream_guard(
        provider_id,
        PROVIDERS[provider_id].content(external_id, http),
        f"content id={content_id}",
        exc_handler=_content_provider_error,
    )
    if SETTINGS.block_russian and is_blocked_country(resp.country):
        _blocklist_cache.set(cache_key, True)
        log.info("blocked Russian content id=%s country=%s", content_id, resp.country)
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    # Stateless per-item group key (issue #69): pure function of the item's
    # own title/type/year, so client state survives across sessions and
    # provider-set changes.
    resp.group_key = group_key_from(resp.title, resp.type, resp.year, content_id)
    _content_cache.set(cache_key, resp)
    return resp


async def _content_by_group_key(group_key: str) -> GroupContentResponse:
    """Look up a merged item by its stateless group key (issue #70).

    Resolves against the cached HomeResponse — the home cache IS the
    source of truth for group_key → providers mapping. This pins the
    staleness behaviour: a key absent from the cached home is 404 for
    up to 30 minutes after the home was last populated, then expires
    and the next /api/home refresh repopulates the mapping. The spec
    accepts this in exchange for the documented 30-min home cache.
    """
    home = _catalog_get_home()
    if home is not None:
        for row in home.rows:
            for it in row.items:
                if it.group_key == group_key:
                    return GroupContentResponse(
                        item=it,
                        providers=list(it.providers),
                    )
    raise HTTPException(
        404,
        detail=ErrorResponse(error="not_found", message=group_key).model_dump(),
    )


async def _content_by_group_key_and_source(
    group_key: str, source: str
) -> GroupSourceContentResponse:
    """Lazy single-source content fetch (issue #60 / v3 spec §3.3).

    Translates the stateless group key into the provider-scoped content
    id (populated by ``/api/home`` from the raw SearchResult listings),
    then issues EXACTLY ONE upstream ``content()`` call against the
    chosen provider. Returns that source's v2 ContentResponse + a
    ``sources`` echo (the §3.2 grouped-card shape) for the source-
    switching chip strip.

    Error semantics:
      - 400 ``unknown_source`` when ``source`` is not one of the group's
        providers (the provider might be real, just doesn't carry this
        group or was retired from the registry after /api/home ran).
      - 502 ``upstream_unreachable`` (with ``sources`` echo) when the
        upstream ``content()`` raises — the chip strip stays up.
      - 404 ``not_found`` when the group key itself is unknown (no entry
        in the sources side cache).
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        raise HTTPException(
            404,
            detail=ErrorResponse(error="not_found", message=group_key).model_dump(),
        )

    # First-seen order: matches the home row's ``HomeItem.providers``
    # because both reads walk the same build_home_rows iteration order.
    sources_echo = list(per_provider.values())
    if source not in per_provider or source not in PROVIDERS:
        raise HTTPException(
            400,
            detail=ErrorResponse(
                error="unknown_source",
                message=f"{source} not in group {group_key}",
            ).model_dump(),
        )

    external_id = per_provider[source].id
    provider = PROVIDERS[source]
    http = get_client()

    try:
        resp = await _upstream_guard(
            source,
            provider.content(external_id, http),
            f"content groupKey={group_key} source={source}",
            exc_handler=_content_provider_error,
        )
    except HTTPException as e:
        # The guard raises 502 with ``{"error": "upstream_unreachable",
        # "message": ...}``. Re-raise with the spec-required ``sources``
        # echo so the UI can degrade just the dead chip. The echo is
        # JSON-serialized as a plain list of dicts because FastAPI's
        # HTTPException detail is encoded by ``json.dumps`` directly
        # (no Pydantic reduction).
        _inject_sources_into_unavailable_error(e, sources_echo)
        raise

    # Re-derive the group key on this single-source response so the
    # returned ContentResponse is self-identifying (issue #69 stateless
    # identity — same key the merge core would compute for this item).
    resp.group_key = group_key_from(resp.title, resp.type, resp.year, resp.id)
    return GroupSourceContentResponse(
        **resp.model_dump(),
        sources=sources_echo,
    )


def _inject_sources_into_unavailable_error(
    exc: HTTPException, sources: list[SearchResult]
) -> None:
    """Add the spec-required ``sources`` echo to an upstream-guard 502.

    Called from the 502 re-raise path so the chip strip stays up even
    when the focused source's ``content()`` raised. The echo is
    JSON-serialized as a plain list of dicts because FastAPI's
    HTTPException detail is encoded by ``json.dumps`` directly (no
    Pydantic reduction). If the upstream detail is not a dict, the
    function is a no-op — the caller will re-raise the original
    exception unchanged.
    """
    if not isinstance(exc.detail, dict):
        return
    new_detail = dict(exc.detail)
    new_detail["sources"] = [s.model_dump() for s in sources]
    exc.detail = new_detail


@app.get("/api/stream/{content_id:path}")
async def stream(content_id: str, translation: str | None = None) -> StreamResponse:
    provider_id, rest = _split_content_id(content_id)
    if provider_id not in PROVIDERS or not rest:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    provider = PROVIDERS[provider_id]
    http = get_client()
    # Episode-level translation validation (issue #9): if the provider
    # reports a known per-episode translation list, reject unknown values
    # before we even hit the network for the stream URL.
    if translation is not None:
        try:
            allowed = await provider.episode_translations(rest, http)
        except Exception:
            allowed = None
        if allowed is not None and translation not in allowed:
            raise HTTPException(
                400,
                detail=ErrorResponse(
                    error="invalid_translation",
                    message=f"{translation} not in {allowed}",
                ).model_dump(),
            )
    resp = await _upstream_guard(
        provider_id,
        provider.stream(rest, translation, http),
        f"stream id={content_id}",
        exc_handler=_stream_provider_error,
    )
    return resp


@app.get("/api/poster")
async def poster(u: str = Query(...)) -> Response:
    # FastAPI already percent-decodes the query param once; the value is
    # the canonical poster URL as stored. No second unquote here — it
    # would corrupt URLs that legitimately contain "%" (e.g. "%20").
    fetched = await fetch_poster(u, get_client())
    if fetched is None:
        raise HTTPException(404, detail=ErrorResponse(error="poster_unavailable", message=u).model_dump())
    body, ctype = fetched
    return Response(content=body, media_type=ctype)
