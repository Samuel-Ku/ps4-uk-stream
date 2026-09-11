from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

import cs_uk_api.providers._registry  # noqa: F401

from . import catalog, lifecycle
from . import config as _config
from . import watchdog as watchdog_mod
from .filters import parse_form_filter as _parse_form_filter
from .filters import parse_style_filter as _parse_style_filter
from .health import TRACKER
from .http_client import get_client
from .jellyfin import router as jellyfin_router
from .jellyfin.capture import capture_request
from .jellyfin.router import normalize_jellyfin_path
from .models import (
    STATUS_DOWN,
    STATUS_WARMING,
    ErrorResponse,
    HealthStatus,
    HomeResponse,
    MediaForm,
    MediaStyle,
    ProviderInfo,
    ProviderSections,
    SearchResponse,
)
from .native import content as native_content
from .poster_proxy import fetch as fetch_poster
from .providers import PROVIDERS
from .providers.base import BaseProvider
from .torrent_engine import ENGINE_TRACKER_ID, engine_configured
from .uakino_browser import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cs_uk_api")

app = FastAPI(title="cs-uk-api", version="0.1.0", lifespan=lifecycle.lifespan)

# Jellyfin facade (spec #100): mounted at the Jellyfin paths, deliberately
# NOT under /api/* — the native contract is untouched and a Jellyfin
# client pointed at host:port finds a server without configuration.
app.include_router(jellyfin_router)
# Native-surface conversations (2026-09-08 review, candidate 1): the
# content conversation (browse / content / stream) lives beside its
# facade siblings under native/, registered on the app directly.
native_content.register(app)


@app.middleware("http")
async def jellyfin_case_normalize(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
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
async def log_requests(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
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


@app.get("/api/health")
async def health() -> dict[str, object]:
    """Backend + provider health snapshot (ticket #215).

    Returns per-provider status (the same ``/api/providers`` view),
    whether ALL non-marker providers are simultaneously down (the wedge
    signal — never a legit steady state), and the watchdog's reset
    counter/last-reset time so an external supervisor can decide to
    restart the process.
    """
    statuses = {
        p.id: _provider_status(p.id)
        for p in PROVIDERS.values()
    }
    # The engine's own tracker entry (spec #394), same visibility rule
    # as /api/providers: present only when the engine is configured.
    if engine_configured(_config.SETTINGS):
        statuses[ENGINE_TRACKER_ID] = _provider_status(ENGINE_TRACKER_ID)
    return {
        "providers": statuses,
        "all_down": watchdog_mod.WATCHDOG.all_relevant_down(),
        "watchdog": {
            "reset_count": watchdog_mod.WATCHDOG.reset_count,
            "last_reset_at": watchdog_mod.WATCHDOG.last_reset_at,
            "cooldown_s": watchdog_mod.WATCHDOG.cooldown_s,
        },
        "recommendations": catalog.recommendation_stats(),
        "catalog_warm": (
            {
                "status": warm_state.status,
                "home_warmed": warm_state.home_warmed,
                "content_warmed": warm_state.content_warmed,
                "planned": warm_state.planned,
                "failed": warm_state.failed,
                "cold_keys": warm_state.cold_keys,
            }
            if (warm_state := lifecycle.catalog_warm_state()) is not None
            else {
                # Ticket #224: a disabled warm (CS_UK_CATALOG_WARM=0) is
                # NOT "pending" — it will never finish, so the runner's
                # warm gate must not block on it. Report done: there is
                # nothing to wait for.
                "status": "done" if not _config.SETTINGS.catalog_warm_enabled else "pending",
                "home_warmed": False,
                "content_warmed": 0,
                "planned": 0,
                "failed": 0,
                "cold_keys": [],
            }
        ),
    }


@app.get("/api/providers")
async def list_providers() -> list[ProviderInfo]:
    entries = [
        ProviderInfo(
            id=p.id,
            name=p.name,
            # Model B capabilities (contract #135): derive the provider's
            # form/styles rollup from its internal classification — the
            # legacy single ``types`` axis is gone from the wire.
            forms=_provider_forms(p),
            styles=_provider_styles(p),
            status=_provider_status(p.id),
            last_error_at=TRACKER.last_error_at(p.id),
        )
        for p in PROVIDERS.values()
    ]
    # The engine's own tracker entry (spec #394): NOT a registry
    # provider — it rides after the registry, only when the engine is
    # configured. Unconfigured = invisible (a deployment choice).
    if engine_configured(_config.SETTINGS):
        entries.append(
            ProviderInfo(
                id=ENGINE_TRACKER_ID,
                name="BitPlay engine",
                forms=[],
                styles=[],
                status=_provider_status(ENGINE_TRACKER_ID),
                last_error_at=TRACKER.last_error_at(ENGINE_TRACKER_ID),
            )
        )
    return entries


def _provider_forms(p: BaseProvider) -> list[MediaForm]:
    """The provider's ``MediaForm`` rollup, deduped in a stable order."""
    seen: list[MediaForm] = []
    for kind in p.types:
        form: MediaForm = "movie" if kind == "movie" else "series"
        if form not in seen:
            seen.append(form)
    return seen


def _provider_styles(p: BaseProvider) -> list[MediaStyle]:
    """The provider's style-tag rollup (∅ on the wire when none)."""
    styles: set[MediaStyle] = set()
    for kind in p.types:
        if kind == "anime" or kind == "cartoon" or kind == "dorama":
            styles.add(kind)
    return sorted(styles)


def _provider_status(provider_id: str) -> HealthStatus:
    """Per-provider status for /api/providers (issue #193).

    A startup marker or a down sliding-window wins outright. Otherwise a
    uakino session that has not finished warming reports the transient
    ``warming`` status; once ready the sliding-window value takes over.
    """
    status = TRACKER.status(provider_id)
    if status == STATUS_DOWN:
        return status
    if provider_id == "uakino" and not get_session().ready_event.is_set():
        return STATUS_WARMING
    return status


@app.get("/api/sections")
async def list_sections() -> list[ProviderSections]:
    """Return only providers that opt into section browsing."""
    return [
        ProviderSections(provider=p.id, name=p.name, sections=list(p.sections))
        for p in PROVIDERS.values()
        if p.sections
    ]


@app.get("/api/search", response_model=SearchResponse, response_model_exclude_unset=True)
async def search(
    q: str = Query(min_length=1, max_length=80),
    provider: str = Query("all"),
    form: str | None = Query(default=None),
    style: str | None = Query(default=None),
) -> SearchResponse:
    """Multi-provider search with per-provider failure attribution (ADR-0002).

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

    The fan-out, merge, gating, cache, and uakino lifecycle live in the
    shared ``_catalog_state.merged_search`` (ticket #106) — the Jellyfin
    facade feeds the same search, so both surfaces share one cache and
    one behaviour.
    """
    if provider != "all" and provider not in PROVIDERS:
        raise HTTPException(400, detail=ErrorResponse(error="unknown_provider", message=provider).model_dump())
    form_filter = _parse_form_filter(form)
    style_filter = _parse_style_filter(style)
    return await catalog.search(
        q, provider=provider, form=form_filter, style_filter=style_filter
    )


@app.get("/api/home", response_model=HomeResponse)
async def home() -> HomeResponse:
    """Merged home view (issue #70).

    Composition:

      - «Нещодавно додані: Фільми» / «: Серіали» (spec #263) — the
        form-split rows that replaced the retired «Новинки» rail:
        providers' newest listings filtered by form, round-robin
        deduped, topped up from the form-section page-1 items when
        under the cap.
      - «Популярні зараз» — only when animeon's ``popular`` section
        returns at least one item (spec AC: present iff animeon
        provides it).
      - Five type rows (movie, series, anime, cartoon, dorama) — each
        aggregates every provider section whose Model B axes
        (``form``/``styles``) map to that kind (``section_row_type``).
        Empty types are omitted.
      - The personalized rows (#252) and the genre rails (#263) are
        computed at build time from the warm content profiles; with no
        profile signal they are omitted.

    Cached for ``SETTINGS.cache_home_s`` (30 minutes by default). On a
    cache hit the providers are not re-invoked.

    Shared with the Jellyfin facade since ticket #101: the build runs in
    ``_catalog_state.load_home`` so the facade resolves the same snapshot.
    """
    return await catalog.refresh_snapshot()


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
