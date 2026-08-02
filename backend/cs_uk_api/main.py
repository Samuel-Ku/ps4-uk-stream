from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .cache import TtlCache
from .config import SETTINGS
from .country import is_blocked_country
from .health import TRACKER
from .http_client import close_client, get_client
from .merge import effective_year, group_key_from
from .models import (
    BrowseResponse,
    ContentResponse,
    ErrorResponse,
    ProviderInfo,
    ProviderSections,
    SearchResponse,
    StreamResponse,
)
from .poster_proxy import fetch as fetch_poster
from .providers import PROVIDERS  # noqa: F401  (import for side effects)
from .providers.base import ProviderError
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
    try:
        results, has_next = await p.browse(section, page, get_client())
    except Exception as e:
        TRACKER.record(provider, ok=False)
        log.warning("browse failed provider=%s section=%s page=%d err=%s", provider, section, page, e)
        raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
    TRACKER.record(provider, ok=True)
    resp = BrowseResponse(provider=provider, section=section, page=page, has_next=has_next, results=results)
    _browse_cache.set(cache_key, resp)
    return resp


@app.get("/api/search")
async def search(
    q: str = Query(min_length=1, max_length=80),
    provider: str = Query("all"),
) -> SearchResponse:
    if provider != "all" and provider not in PROVIDERS:
        raise HTTPException(400, detail=ErrorResponse(error="unknown_provider", message=provider).model_dump())
    cache_key = f"search:{provider}:{q}"
    cached = _search_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    selected = PROVIDERS.values() if provider == "all" else [PROVIDERS[provider]]
    http = get_client()

    async def run(p):
        try:
            results = await p.search(q, http)
        except Exception as e:  # noqa: BLE001
            TRACKER.record(p.id, ok=False)
            log.warning("provider=%s error=%s", p.id, e)
            return []
        TRACKER.record(p.id, ok=True)
        return results

    results_lists = await asyncio.wait_for(
        asyncio.gather(*(run(p) for p in selected)),
        timeout=SETTINGS.search_total_timeout_s,
    )
    flat = [item for sub in results_lists for item in sub]
    resp = SearchResponse(query=q, results=flat)
    _search_cache.set(cache_key, resp)
    return resp


@app.get("/api/content/{content_id:path}")
async def content(content_id: str) -> ContentResponse:
    cache_key = f"content:{content_id}"
    if _blocklist_cache.get(cache_key) is not None:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    cached = _content_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    provider_id, _, external_id = content_id.partition(":")
    if provider_id not in PROVIDERS or not external_id:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    http = get_client()
    try:
        resp = await PROVIDERS[provider_id].content(external_id, http)
    except Exception as e:
        TRACKER.record(provider_id, ok=False)
        log.warning("content failed id=%s err=%s", content_id, e)
        raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
    TRACKER.record(provider_id, ok=True)
    if SETTINGS.block_russian and is_blocked_country(resp.country):
        _blocklist_cache.set(cache_key, True)
        log.info("blocked Russian content id=%s country=%s", content_id, resp.country)
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    # Stateless per-item group key (issue #69): pure function of the item's
    # own title/type/year, so client state survives across sessions and
    # provider-set changes.
    resp.group_key = group_key_from(resp.title, resp.type, effective_year(resp.title, resp.year), content_id)
    _content_cache.set(cache_key, resp)
    return resp


@app.get("/api/stream/{content_id:path}")
async def stream(content_id: str, translation: str | None = None) -> StreamResponse:
    provider_id, _, rest = content_id.partition(":")
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
    try:
        resp = await provider.stream(rest, translation, http)
    except ProviderError as e:
        # Translation-level validation errors are client-side semantics,
        # not upstream-health signals — they must not move the needle.
        if e.code == "invalid_translation":
            raise HTTPException(400, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e
        if e.code == "translation_missing":
            raise HTTPException(404, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e
        TRACKER.record(provider_id, ok=False)
        log.warning("stream failed id=%s err=%s", content_id, e)
        raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
    except Exception as e:
        TRACKER.record(provider_id, ok=False)
        log.warning("stream failed id=%s err=%s", content_id, e)
        raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
    TRACKER.record(provider_id, ok=True)
    return resp


@app.get("/api/poster")
async def poster(u: str = Query(...)) -> Response:
    raw = unquote(u)
    fetched = await fetch_poster(raw, get_client())
    if fetched is None:
        raise HTTPException(404, detail=ErrorResponse(error="poster_unavailable", message=raw).model_dump())
    body, ctype = fetched
    return Response(content=body, media_type=ctype)
