"""The native surface's content conversation (2026-09-08 review, cand. 1).

ONE owner for the routes that turn an id into a playable answer:
``/api/browse``, the ``/api/content/{id}`` discriminator and its three
lookup legs, and ``/api/stream/{id}`` with the translation-validation
gate. Moved verbatim from ``main.py`` (PR #411 proved the split pattern
on the facade; this is the native surface's turn). ``main.py`` keeps
assembly: app wiring, middleware, health/providers/sections/search/
home/poster.

Import direction: this module -> catalog (the typed seam),
service (error vocabulary), providers — never main.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException, Query

from .. import catalog
from ..catalog import GATE_CHECK_TIMEOUT_S
from ..catalog import await_uakino_ready as _await_uakino_ready
from ..catalog import browse_cache as _browse_cache
from ..catalog import filter_gated_items as _filter_gated_items
from ..filters import section_matches as _section_matches
from ..http_client import get_client
from ..merge import group_key_from
from ..models import (
    BrowseResponse,
    ContentResponse,
    ErrorResponse,
    GroupContentResponse,
    GroupSourceContentResponse,
    StreamResponse,
)
from ..providers import PROVIDERS
from ..service import (
    content_provider_error as _content_provider_error,
)
from ..service import (
    inject_sources_into_unavailable_error as _inject_sources_into_unavailable_error,
)
from ..service import split_content_id as _split_content_id
from ..service import stream_provider_error as _stream_provider_error
from ..service import upstream_guard as _upstream_guard
from ..wire_identity import is_group_key

log = logging.getLogger("cs_uk_api")


def register(app: FastAPI) -> None:
    """Attach the content conversation to the native app.

    Route declaration order relative to ``main.py`` is preserved
    (browse before content before stream) so the route table is
    identical.
    """

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


# ---------------------------------------------------------------------------
# The content legs (module-level: the discriminator above routes to them)
# ---------------------------------------------------------------------------


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

    Spec #364 bug fix: resolves via the shared group-resolution lookup
    (ADR-0010), not a snapshot scan — a search-found title
    absent from the 30-min home snapshot now resolves instead of 404ing
    for up to 30 min while the facade already shows it. Wire shape
    unchanged (GroupContentResponse{item, providers}); only the lookup
    source moves.
    """
    res = catalog.group_resolution(group_key)
    if res is not None and res.card is not None:
        return GroupContentResponse(item=res.card, providers=list(res.providers))
    if res is not None and res.sources:
        first = res.sources[0]
        providers = list(res.providers)
        from ..models import HomeItem

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
    res = catalog.group_resolution(group_key)
    if res is None or not res.sources:
        raise HTTPException(
            404,
            detail=ErrorResponse(error="not_found", message=group_key).model_dump(),
        )
    sources_echo = list(res.sources)

    # First-seen order: matches the home row's ``HomeItem.providers``
    # because both reads walk the same build_home_rows iteration order.
    card = res.source(source)
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
