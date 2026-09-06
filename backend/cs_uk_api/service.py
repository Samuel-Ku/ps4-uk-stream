"""Service-layer helpers shared by the native routes (and, later, the
Jellyfin facade): the upstream-guard pattern and error translation.

Extracted from the route module (main.py) so ADR-0002's failure
semantics — "record health, translate client-visible errors, never let a
gated/invalid-translation error move the tracker" — live in one testable
place instead of being hand-rolled at every call site.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from fastapi import HTTPException

from .health import TRACKER, record_verdict
from .models import ErrorResponse, SearchResult
from .providers.base import ProviderError
from .torrent_engine import ENGINE_TRACKER_ID

log = logging.getLogger("cs_uk_api")

T = TypeVar("T")

#: Sentinel for ``upstream_guard(..., on_error=...)``: distinguishes the
#: parameter's "no default provided" state from a legitimate ``None``
#: default. Callers that want to degrade to None on upstream failure
#: must pass ``on_error=None`` explicitly; omitting the kwarg means
#: "raise 502".
_UNSET: object = object()


def split_content_id(content_id: str) -> tuple[str, str]:
    """Content id "provider:external" -> (provider, external).

    The named accessor for the provider-by-prefix derivation shared by
    the content and stream routes; malformed ids yield ("", "").
    """
    provider_id, _, external_id = content_id.partition(":")
    return provider_id, external_id


async def upstream_guard(
    provider_id: str,
    coro: Awaitable[T],
    log_label: str,
    *,
    on_error: T | object = _UNSET,
    exc_handler: Callable[[Exception], None] | None = None,
    record_skip_codes: frozenset[str] = frozenset(),
) -> T:
    """Await an upstream provider call with health recording + the 502 guard.

    The record+log+raise(502) pattern shared by every upstream try/except
    site lives here and nowhere else. The failure path runs in this order:

      1. ``exc_handler(e)`` — if provided, runs first. It either raises
         (translating the upstream error into a client-side response such
         as 400/404) or returns. The helper does NOT record when the
         handler raises; translation-level errors are not upstream-health
         signals.
      2. ``TRACKER.record(provider_id, ok=False)`` + warning log —
         skipped when the error is a ProviderError whose ``code`` is in
         ``record_skip_codes`` (deterministic item-level verdicts are not
         lane faults; the wire envelope is unchanged).
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
        if isinstance(e, ProviderError) and e.code in record_skip_codes:
            log.warning("%s deterministic verdict provider=%s err=%s", log_label, provider_id, e)
            raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
        # The shared classification (health.py) is the backstop: callers
        # may ADD skip codes, never override the base rule that item-level
        # verdicts (ADR-0002) are not lane faults. Engine-path faults
        # (spec #394) retarget to the yts:engine tracker entry.
        record_verdict(
            ENGINE_TRACKER_ID
            if isinstance(e, ProviderError) and e.engine_path
            else provider_id,
            e.code if isinstance(e, ProviderError) else None,
        )
        log.warning("%s failed provider=%s err=%s", log_label, provider_id, e)
        if on_error is _UNSET:
            raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
        # The sentinel check above guarantees this is a real T (the caller
        # passed an explicit default), but mypy cannot narrow ``T | object``
        # to ``T`` from ``is not _UNSET`` alone.
        return cast(T, on_error)
    TRACKER.record(provider_id, ok=True)
    return result


def content_provider_error(e: Exception) -> None:
    """Subscription-gated content is a client-visible 404, not an
    upstream-health signal — the item is deliberately unavailable."""
    if isinstance(e, ProviderError) and e.code == "gated":
        raise HTTPException(404, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e


def stream_provider_error(e: Exception) -> None:
    """Translation-level validation errors are client-side semantics, not
    upstream-health signals — they must not move the needle. A gated
    stream is a deliberate "no playable file" verdict → 404."""
    if not isinstance(e, ProviderError):
        return
    if e.code == "invalid_translation":
        raise HTTPException(400, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e
    if e.code in ("translation_missing", "gated"):
        raise HTTPException(404, detail=ErrorResponse(error=e.code, message=e.message).model_dump()) from e


def inject_sources_into_unavailable_error(
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


__all__ = [
    "content_provider_error",
    "inject_sources_into_unavailable_error",
    "split_content_id",
    "stream_provider_error",
    "upstream_guard",
]
