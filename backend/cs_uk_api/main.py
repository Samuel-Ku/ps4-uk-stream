from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

import cs_uk_api.providers._registry  # noqa: F401

from . import catalog, lifecycle
from . import config as _config
from . import watchdog as watchdog_mod
from .cache import TtlCache
from .catalog import GATE_CHECK_TIMEOUT_S
from .catalog import await_uakino_ready as _await_uakino_ready
from .catalog import filter_gated_items as _filter_gated_items
from .config import SETTINGS
from .filters import parse_form_filter as _parse_form_filter
from .filters import parse_style_filter as _parse_style_filter
from .filters import section_matches as _section_matches
from .health import TRACKER
from .http_client import get_client
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
from .service import (
    content_provider_error as _content_provider_error,
)
from .service import inject_sources_into_unavailable_error as _inject_sources_into_unavailable_error
from .service import split_content_id as _split_content_id
from .service import stream_provider_error as _stream_provider_error
from .service import upstream_guard as _upstream_guard
from .torrent_engine import ENGINE_TRACKER_ID, engine_configured
from .uakino_browser import get_session
from .wire_identity import is_group_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cs_uk_api")

#: The browse route's own TtlCache (ADR-0003 browse TTL) — the one cache
#: main owns outright; every other catalog cache lives in
#: ``_catalog_state`` behind the ``catalog`` interface (spec #309 T4).
_browse_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)

app = FastAPI(title="cs-uk-api", version="0.1.0", lifespan=lifecycle.lifespan)

# Jellyfin facade (spec #100): mounted at the Jellyfin paths, deliberately
# NOT under /api/* — the native contract is untouched and a Jellyfin
# client pointed at host:port finds a server without configuration.
app.include_router(jellyfin_router)


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
                timeout=GATE_CHECK_TIMEOUT_S,
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
    if is_group_key(content_id):
        if source is not None:
            return await _content_by_group_key_and_source(content_id, source)
        return await _content_by_group_key(content_id)
    return await _content_by_id(content_id)


async def _content_by_id(content_id: str) -> ContentResponse:
    provider_id, external_id = _split_content_id(content_id)
    if provider_id not in PROVIDERS or not external_id:
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    if provider_id == "uakino":
        await _await_uakino_ready()
    # The cache layer, the gated/blocklist verdict stores and the
    # group-key derivation live behind the catalog seam (spec #309 T4):
    # main no longer constructs ``content:`` keys or reads the stores.
    result = await _upstream_guard(
        provider_id,
        catalog.provider_content(provider_id, external_id),
        f"content id={content_id}",
        exc_handler=_content_provider_error,
    )
    if result.verdict is catalog.ContentVerdict.GATED:
        raise HTTPException(404, detail=ErrorResponse(error="gated", message=content_id).model_dump())
    if result.verdict is not catalog.ContentVerdict.OK:
        # Blocklisted (or otherwise deliberately unavailable) — the same
        # not_found the pre-T4 route answered.
        raise HTTPException(404, detail=ErrorResponse(error="not_found", message=content_id).model_dump())
    return result.content  # type: ignore[return-value]


async def _content_by_group_key(group_key: str) -> GroupContentResponse:
    """Look up a merged item by its stateless group key (issue #70, #364).

    Spec #364 bug fix: resolves via the shared resolution map (the index
    beside sources_cache), not a snapshot scan — a search-found title
    absent from the 30-min home snapshot now resolves instead of 404ing
    for up to 30 min while the facade already shows it. Wire shape
    unchanged (GroupContentResponse{item, providers}); only the lookup
    source moves.
    """
    card = catalog.card_for_group(group_key)
    if card is not None:
        return GroupContentResponse(item=card, providers=list(card.providers))
    sources = catalog.group_sources(group_key)
    if sources:
        first = sources[0]
        providers = [s.provider for s in sources]
        from .models import HomeItem

        item = HomeItem(
            group_key=group_key,
            title=first.title,
            year=first.year,
            poster=first.poster,
            form=first.form,
            styles=first.styles,
            genres=list(first.genres),
            providers=providers,
            member_keys=[group_key],
        )
        return GroupContentResponse(item=item, providers=providers)
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
    sources_echo = catalog.group_sources(group_key)
    if not sources_echo:
        raise HTTPException(
            404,
            detail=ErrorResponse(error="not_found", message=group_key).model_dump(),
        )

    # First-seen order: matches the home row's ``HomeItem.providers``
    # because both reads walk the same build_home_rows iteration order.
    card = catalog.group_source(group_key, source)
    if card is None or source not in PROVIDERS:
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
    _, external_id = _split_content_id(card.id)
    if not external_id:
        raise HTTPException(
            404,
            detail=ErrorResponse(
                error="not_found", message=card.id
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
            log.warning(
                "episode_translations(%s) failed; accepting any translation",
                provider_id,
                exc_info=True,
            )
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
        # Deterministic item-level verdicts (dead torrent, bad external
        # id) must not poison lane health — the envelope stays the
        # canonical 502 (ADR-0002 spirit: verdicts ≠ infra faults).
        record_skip_codes=frozenset({"not_found"}),
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
