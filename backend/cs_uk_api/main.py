from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

import cs_uk_api.providers._registry  # noqa: F401

from .cache import TtlCache
from .catalog_state import _GATE_CHECK_TIMEOUT_S, resolve_group
from .catalog_state import await_uakino_ready as _await_uakino_ready
from .catalog_state import blocklist_cache as _catalog_blocklist_cache
from .catalog_state import content_cache as _catalog_content_cache
from .catalog_state import filter_gated_items as _filter_gated_items
from .catalog_state import gated_cache as _catalog_gated_cache
from .catalog_state import get_home as _catalog_get_home
from .catalog_state import home_cache as _catalog_home_cache
from .catalog_state import load_home as _catalog_load_home
from .catalog_state import merged_search as _catalog_merged_search
from .catalog_state import search_cache as _catalog_search_cache
from .catalog_state import sources_cache as _catalog_sources_cache
from .config import SETTINGS
from .country import is_blocked_country
from .filters import parse_style_filter as _parse_style_filter
from .filters import section_matches as _section_matches
from .health import TRACKER
from .http_client import close_client, get_client
from .jellyfin import router as jellyfin_router
from .jellyfin.capture import capture_request
from .jellyfin.router import normalize_jellyfin_path
from .merge import group_key_from
from .models import (
    STATUS_DOWN,
    STATUS_WARMING,
    BrowseResponse,
    ContentResponse,
    ErrorResponse,
    GroupContentResponse,
    GroupSourceContentResponse,
    HealthStatus,
    HomeResponse,
    MediaForm,
    MediaStyle,
    ProviderInfo,
    ProviderSections,
    SearchResponse,
    StreamResponse,
)
from .poster_proxy import fetch as fetch_poster
from .providers import PROVIDERS
from .providers.base import BaseProvider
from .providers.base import model_b_axes as _model_b_axes
from .service import (
    content_provider_error as _content_provider_error,
)
from .service import inject_sources_into_unavailable_error as _inject_sources_into_unavailable_error
from .service import split_content_id as _split_content_id
from .service import stream_provider_error as _stream_provider_error
from .service import upstream_guard as _upstream_guard
from .uakino_browser import DEFAULT_CHROMIUM, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cs_uk_api")

# uakino's browser-session provider cannot work without a system Chromium
# binary (v3 spec §2.1): mark it down deterministically at startup instead
# of letting it fail per-request.
if not os.path.exists(DEFAULT_CHROMIUM):
    TRACKER.mark_startup("uakino", "chromium_missing")
    log.warning("uakino marked down at startup: chromium binary not found at %s", DEFAULT_CHROMIUM)

#: The shared merged-search cache now lives in ``catalog_state``
#: (ticket #106: the native /api/search route and the Jellyfin facade
#: share one fan-out and one cache). ``_search_cache`` stays as a
#: back-compat alias — tests import it from here.
_search_cache = _catalog_search_cache
_content_cache = _catalog_content_cache
_browse_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)
_blocklist_cache = _catalog_blocklist_cache

#: Back-compat aliases (tests import these): the home snapshot + group-key
#: resolution map now live in ``cs_uk_api.catalog_state`` (ticket #101),
#: shared by the native ``/api/*`` routes and the Jellyfin facade.
#: Clearing them clears the shared state.
_home_cache = _catalog_home_cache
_home_sources_cache = _catalog_sources_cache

#: Bounded drain for the background warm/heartbeat task in lifespan
#: shutdown so a mid-warm Chromium launch cannot hang the teardown.
_WARM_TASK_DRAIN_S: float = 1.0

#: Handle of the background warm+heartbeat task started by ``lifespan``.
_warm_task: asyncio.Task[None] | None = None


async def _warm_and_heartbeat() -> None:
    """Background uakino warm + heartbeat (issue #193/#195).

    Scheduled once by ``lifespan``. ``warm()`` failures are pinned as
    deterministic startup markers so explicit uakino routes short-circuit
    502 instead of blocking on a session that can never serve; success
    hands off to the heartbeat loop, which records ok/fail per tick into
    TRACKER — the sliding-window state ``/api/providers`` and the fan-out
    skip read. Cancelled by ``lifespan`` shutdown.
    """
    session = get_session()
    try:
        await session.warm()
    except TimeoutError:
        TRACKER.mark_startup("uakino", "warm_timeout")
        log.warning("uakino warm timed out; marked down at startup")
        return
    except Exception as e:  # noqa: BLE001
        TRACKER.mark_startup("uakino", "warm_failed")
        log.warning("uakino warm failed; marked down at startup: %s", e)
        return
    await session.heartbeat_loop(record=lambda ok: TRACKER.record("uakino", ok))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _warm_task
    if os.path.exists(DEFAULT_CHROMIUM):
        # Background warm+heartbeat (issue #193): uakino's browser session
        # is brought up once at startup instead of lazily on first request,
        # so its health is known before a client asks for it.
        _warm_task = asyncio.create_task(_warm_and_heartbeat())
    yield
    if _warm_task is not None:
        _warm_task.cancel()
        try:
            await asyncio.wait_for(_warm_task, timeout=_WARM_TASK_DRAIN_S)
        except (TimeoutError, asyncio.CancelledError):
            pass
        _warm_task = None
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


def _provider_forms(p: BaseProvider) -> list[MediaForm]:
    """The provider's ``MediaForm`` rollup, deduped in a stable order."""
    seen: list[MediaForm] = []
    for kind in p.types:
        form = _model_b_axes(kind)[0]
        if form not in seen:
            seen.append(form)
    return seen


def _provider_styles(p: BaseProvider) -> list[MediaStyle]:
    """The provider's style-tag rollup (∅ on the wire when none)."""
    styles: set[MediaStyle] = set()
    for kind in p.types:
        styles.update(_model_b_axes(kind)[1])
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

    The fan-out, merge, gating, cache, and uakino lifecycle live in the
    shared ``catalog_state.merged_search`` (ticket #106) — the Jellyfin
    facade feeds the same search, so both surfaces share one cache and
    one behaviour.
    """
    if provider != "all" and provider not in PROVIDERS:
        raise HTTPException(400, detail=ErrorResponse(error="unknown_provider", message=provider).model_dump())
    style_filter = _parse_style_filter(style)
    return await _catalog_merged_search(
        q, provider=provider, form=form, style_filter=style_filter
    )


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
    """Discriminator: ``g2:…`` group keys route to the merged lookup;
    everything else is the existing ``provider:external`` content path.

    For ``g2:…`` keys, an optional ``?source=<provider>`` query param
    routes to the lazy single-source fetch (issue #60 / v3 spec §3.3):
    returns that ONE source's v2 ContentResponse + a ``providers`` echo
    for the source-switching chip strip. Without ``?source=``, the
    legacy ``GroupContentResponse{item, providers}`` shape is returned
    (preserved for backwards compatibility).
    """
    if content_id.startswith("g2:"):
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
    if provider_id == "uakino":
        await _await_uakino_ready()
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
    resp.group_key = group_key_from(resp.title, resp.form, resp.year, content_id)
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

    # SearchResult.id carries the ``<provider>:`` wire prefix; the
    # adapter's ``content()`` expects the bare external id (the same
    # derivation ``_content_by_id`` does via ``_split_content_id``).
    # Issue #157: the lazy branch used to pass the prefixed id straight
    # through, which 502'd for every provider whose content() validates
    # the external id shape.
    _, external_id = _split_content_id(per_provider[source].id)
    if not external_id:
        raise HTTPException(
            404,
            detail=ErrorResponse(
                error="not_found", message=per_provider[source].id
            ).model_dump(),
        )
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
    resp.group_key = group_key_from(resp.title, resp.form, resp.year, resp.id)
    return GroupSourceContentResponse(
        **resp.model_dump(),
        sources=sources_echo,
    )


@app.get("/api/stream/{content_id:path}")
async def stream(content_id: str, translation: str | None = None) -> StreamResponse:
    provider_id, rest = _split_content_id(content_id)
    if provider_id not in PROVIDERS or not rest:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    if provider_id == "uakino":
        await _await_uakino_ready()
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
