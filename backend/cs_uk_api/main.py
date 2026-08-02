from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, TypeVar, cast
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .cache import TtlCache
from .config import SETTINGS
from .country import is_blocked_country
from .health import TRACKER
from .home import build_home_rows
from .http_client import close_client, get_client
from .merge import group_key_from
from .models import (
    BrowseResponse,
    ContentResponse,
    ErrorResponse,
    GroupContentResponse,
    HomeResponse,
    ProviderFailure,
    ProviderInfo,
    ProviderSections,
    SearchResponse,
    SearchResult,
    StreamResponse,
)
from .poster_proxy import fetch as fetch_poster
from .providers import PROVIDERS  # noqa: F401  (import for side effects)
from .providers.base import BaseProvider, ProviderError
from .uakino_browser import DEFAULT_CHROMIUM, get_session
import cs_uk_api.providers._registry  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cs_uk_api")

# uakino's browser-session provider cannot work without a system Chromium
# binary (v3 spec §2.1): mark it down deterministically at startup instead
# of letting it fail per-request.
if not os.path.exists(DEFAULT_CHROMIUM):
    TRACKER.mark_startup("uakino", "chromium_missing")
    log.warning("uakino marked down at startup: chromium binary not found at %s", DEFAULT_CHROMIUM)

_search_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)
_content_cache = TtlCache(default_ttl_s=SETTINGS.cache_content_s)
_browse_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)
_blocklist_cache = TtlCache(default_ttl_s=SETTINGS.cache_content_s)
# v3 (issue #70): the merged home view — «Новинки» + «Популярні зараз»
# + the five type rows — is a curated snapshot, refreshed every 30 min
# per the spec's documented staleness behaviour.
_home_cache = TtlCache(default_ttl_s=SETTINGS.cache_home_s)

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
    except Exception as e:  # noqa: BLE001
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


def _stream_provider_error(e: Exception) -> None:
    """Translation-level validation errors are client-side semantics, not
    upstream-health signals — they must not move the needle."""
    if not isinstance(e, ProviderError):
        return
    if e.code == "invalid_translation":
        raise HTTPException(400, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e
    if e.code == "translation_missing":
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


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    response: Response = await call_next(request)
    latency_ms = int((time.monotonic() - started) * 1000)
    log.info("%s %s -> %s (%dms)", request.method, request.url.path, response.status_code, latency_ms)
    return response


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
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
    resp = BrowseResponse(provider=provider, section=section, page=page, has_next=has_next, results=results)
    _browse_cache.set(cache_key, resp)
    return resp


@app.get("/api/search", response_model=SearchResponse, response_model_exclude_unset=True)
async def search(
    q: str = Query(min_length=1, max_length=80),
    provider: str = Query("all"),
) -> SearchResponse:
    """Multi-provider search with per-provider failure attribution (ADR-0002).

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
    cache_key = f"search:{provider}:{q}"
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
            failures.append(
                ProviderFailure(provider=pid, code="internal", message=str(e))
            )
            continue
        if isinstance(content, ProviderFailure):
            TRACKER.record(pid, ok=False)
            failures.append(content)
        else:
            TRACKER.record(pid, ok=True)
            out_results.extend(content)

    # Pending tasks: the overall budget fired before they completed.
    # Per ADR-0002, each one gets a synthetic ``timeout`` row.
    for task in pending:
        pid = tasks[task]
        failures.append(
            ProviderFailure(
                provider=pid,
                code="timeout",
                message=f"overall budget {SETTINGS.search_total_timeout_s}s exceeded",
            )
        )

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
    if failures:
        resp = SearchResponse(query=q, results=out_results, failures=failures)
    else:
        resp = SearchResponse(query=q, results=out_results)
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
    """
    cache_key = "home:v1"
    cached = _home_cache.get(cache_key)
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
            # Multiple sections from the same provider can map to the
            # same ``type`` (e.g. animeua's ``page``, ``anime``, ``ona``,
            # ``ova`` all type as ``anime``). Extend per-provider rather
            # than overwrite, so all sections contribute to the row.
            buckets = type_lists.setdefault(type_key, {})
            buckets.setdefault(pid, []).extend(results)

    # Fan out: newest sections per opt-in provider, popular (animeon),
    # and every (provider, section) pair for the five type buckets.
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
        # whole /api/home request out to ``upstream_timeout_s * N``. We
        # reuse the search budget for now — both routes fan out to the
        # full provider registry with the same per-provider upstream
        # timeout. The 30-min home cache absorbs the steady-state
        # latency; this budget only matters on cache miss (cold start).
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=False),
                timeout=SETTINGS.search_total_timeout_s,
            )
        except asyncio.TimeoutError:
            # Per-task try/except already swallowed individual
            # failures; the only thing that escapes the gather is a
            # hung upstream. Cancel + drain and accept the rows we
            # already collected.
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Await the cancellation propagation so the event loop
            # is clean before we proceed to row assembly.
            await asyncio.gather(*tasks, return_exceptions=True)
            log.warning("home fan-out hit overall budget providers=%d", len(tasks))

    rows = build_home_rows(
        newest=newest_lists,
        popular=popular_lists,
        by_type=type_lists,
        newest_limit=SETTINGS.home_row_limit,
    )
    resp = HomeResponse(rows=rows)
    _home_cache.set(cache_key, resp)
    return resp


@app.get("/api/content/{content_id:path}")
async def content(content_id: str) -> ContentResponse | GroupContentResponse:
    """Discriminator: ``g1:…`` group keys route to the merged lookup;
    everything else is the existing ``provider:external`` content path."""
    if content_id.startswith("g1:"):
        return await _content_by_group_key(content_id)
    return await _content_by_id(content_id)


async def _content_by_id(content_id: str) -> ContentResponse:
    cache_key = f"content:{content_id}"
    if _blocklist_cache.get(cache_key) is not None:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
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
    home = _home_cache.get("home:v1")
    if home is not None:
        for row in cast(HomeResponse, home).rows:
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
    raw = unquote(u)
    fetched = await fetch_poster(raw, get_client())
    if fetched is None:
        raise HTTPException(404, detail=ErrorResponse(error="poster_unavailable", message=raw).model_dump())
    body, ctype = fetched
    return Response(content=body, media_type=ctype)
