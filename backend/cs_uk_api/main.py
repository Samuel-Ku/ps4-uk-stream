from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .cache import TtlCache
from .config import SETTINGS
from .http_client import close_client, get_client
from .models import (
    ContentResponse,
    ErrorResponse,
    ProviderInfo,
    SearchResponse,
    StreamResponse,
)
from .poster_proxy import fetch as fetch_poster
from .providers import PROVIDERS  # noqa: F401  (import for side effects)
import cs_uk_api.providers._registry  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cs_uk_api")

_search_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)
_content_cache = TtlCache(default_ttl_s=SETTINGS.cache_content_s)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
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
        ProviderInfo(id=p.id, name=p.name, types=list(p.types))  # type: ignore[arg-type]
        for p in PROVIDERS.values()
    ]


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
            return await p.search(q, http)
        except Exception as e:  # noqa: BLE001
            log.warning("provider=%s error=%s", p.id, e)
            return []

    results_lists = await asyncio.wait_for(
        asyncio.gather(*(run(p) for p in selected)),
        timeout=SETTINGS.search_total_timeout_s,
    )
    flat = [item for sub in results_lists for item in sub]
    resp = SearchResponse(query=q, results=flat)
    _search_cache.set(cache_key, resp)
    return resp


@app.get("/api/content/{content_id}")
async def content(content_id: str) -> ContentResponse:
    cache_key = f"content:{content_id}"
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
        log.warning("content failed id=%s err=%s", content_id, e)
        raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
    _content_cache.set(cache_key, resp)
    return resp


@app.get("/api/stream/{content_id}")
async def stream(content_id: str, translation: str | None = None) -> StreamResponse:
    provider_id, _, rest = content_id.partition(":")
    if provider_id not in PROVIDERS or not rest:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    http = get_client()
    try:
        return await PROVIDERS[provider_id].stream(rest, translation, http)
    except Exception as e:
        log.warning("stream failed id=%s err=%s", content_id, e)
        raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e


@app.get("/api/poster")
async def poster(u: str = Query(...)) -> Response:
    raw = unquote(u)
    fetched = await fetch_poster(raw, get_client())
    if fetched is None:
        raise HTTPException(404, detail=ErrorResponse(error="poster_unavailable", message=raw).model_dump())
    body, ctype = fetched
    return Response(content=body, media_type=ctype)
