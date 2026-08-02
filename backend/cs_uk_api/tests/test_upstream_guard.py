"""Tests for _upstream_guard (issue #92).

The helper is the single source of the "record + log + 502-or-degrade"
pattern shared by every upstream try/except site in main.py. The contract:

  - Success path: ``TRACKER.record(provider_id, ok=True)`` + return result.
  - Failure path: ``exc_handler(e)`` runs first; if it raises, that
    exception propagates untouched (skips recording). Otherwise:
    ``TRACKER.record(provider_id, ok=False)`` + log + either return
    ``on_error`` (when an explicit default was provided) or raise the
    canonical 502 ``upstream_unreachable``.
  - ``on_error`` uses a sentinel to distinguish "no default" (raise 502)
    from "degrade to None" (return None) — the previous ``None`` default
    conflated these two cases.

These tests pin the contract down at the helper's surface so the
call sites can stay terse.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from cs_uk_api import health
from cs_uk_api.main import _upstream_guard
from cs_uk_api.providers.base import ProviderError


@pytest.fixture(autouse=True)
def reset_tracker():
    health.TRACKER.reset()
    yield
    health.TRACKER.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ok_coro(value):
    return value


async def _boom_coro():
    raise RuntimeError("upstream is down")


def _samples_for(pid: str) -> list[bool]:
    samples = health.TRACKER._samples.get(pid)
    return list(samples) if samples is not None else []


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_guard_returns_result_on_success_and_records_ok() -> None:
    out = await _upstream_guard("p", _ok_coro(["a", "b"]), "test label")
    assert out == ["a", "b"]
    assert _samples_for("p") == [True]


# ---------------------------------------------------------------------------
# Failure path: no on_error → 502
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_guard_raises_502_when_no_default_is_provided() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _upstream_guard("p", _boom_coro(), "test label")
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail["error"] == "upstream_unreachable"
    assert "upstream is down" in excinfo.value.detail["message"]


@pytest.mark.asyncio
async def test_upstream_guard_records_failure_on_502_path() -> None:
    with pytest.raises(HTTPException):
        await _upstream_guard("p", _boom_coro(), "test label")
    assert _samples_for("p") == [False]


# ---------------------------------------------------------------------------
# Sentinel: distinguish "no default" from "default is None"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_guard_returns_explicit_none_default_without_raising() -> None:
    """``on_error=None`` (passed explicitly) means "degrade to None" —
    distinct from the parameter's default sentinel ("raise 502"). This
    is the contract change introduced in #92; before the sentinel, the
    helper conflated the two and would have raised 502 here."""
    out = await _upstream_guard("p", _boom_coro(), "test label", on_error=None)
    assert out is None
    assert _samples_for("p") == [False]


@pytest.mark.asyncio
async def test_upstream_guard_returns_explicit_empty_list_default() -> None:
    """Sanity check: ``on_error=[]`` for a list-typed result still
    returns the empty list (the sentinel works for any default value,
    not just None)."""
    out = await _upstream_guard("p", _boom_coro(), "test label", on_error=[])
    assert out == []


# ---------------------------------------------------------------------------
# exc_handler contract: translate (raise) OR fall-through (return)
# ---------------------------------------------------------------------------


def _raise_handler(e: Exception) -> None:
    """Translator: converts the upstream error into a 400 response.
    Raising short-circuits recording."""
    raise HTTPException(400, detail={"error": "client_problem", "message": str(e)})


@pytest.mark.asyncio
async def test_upstream_guard_skips_recording_when_exc_handler_raises() -> None:
    """When exc_handler raises (translates to a client-side response),
    the propagation skips TRACKER.record — translation-level errors
    are not upstream-health signals."""
    with pytest.raises(HTTPException) as excinfo:
        await _upstream_guard(
            "p", _boom_coro(), "test label", exc_handler=_raise_handler
        )
    assert excinfo.value.status_code == 400
    assert _samples_for("p") == []  # no recording


def _no_op_handler(e: Exception) -> None:
    """Returns without raising → exc_handler didn't translate; the
    helper falls through to the record+log+502-or-degrade path."""


@pytest.mark.asyncio
async def test_upstream_guard_records_failure_when_exc_handler_returns() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _upstream_guard(
            "p", _boom_coro(), "test label", exc_handler=_no_op_handler
        )
    assert excinfo.value.status_code == 502
    assert _samples_for("p") == [False]


@pytest.mark.asyncio
async def test_upstream_guard_returns_default_when_exc_handler_returns() -> None:
    """Combined contract: exc_handler returns (no translation) AND
    an explicit on_error default is provided → recording happens
    AND the default is returned."""
    out = await _upstream_guard(
        "p",
        _boom_coro(),
        "test label",
        on_error="degraded",
        exc_handler=_no_op_handler,
    )
    assert out == "degraded"
    assert _samples_for("p") == [False]


# ---------------------------------------------------------------------------
# exc_handler with a real ProviderError (mirrors stream's actual handler)
# ---------------------------------------------------------------------------


def _stream_provider_error(e: Exception) -> None:
    """Mirror of cs_uk_api.main._stream_provider_error: a real-world
    handler that inspects ``ProviderError.code`` and either raises a
    client-side response (400/404) or returns to fall through to the
    record+502 path. Used to exercise the production contract end-to-end
    at the helper's surface."""
    if not isinstance(e, ProviderError):
        return
    if e.code == "invalid_translation":
        raise HTTPException(400, detail={"error": e.code, "message": e.message}) from e
    if e.code == "translation_missing":
        raise HTTPException(404, detail={"error": e.code, "message": e.message}) from e


async def _invalid_translation_coro():
    raise ProviderError("invalid_translation", "en not in [uk]")


async def _translation_missing_coro():
    raise ProviderError("translation_missing", "no translations declared for this episode")


async def _unknown_provider_error_coro():
    """ProviderError with a code the handler doesn't translate — must
    fall through to the record+log+502 path."""
    raise ProviderError("scrape_failed", "site returned 503")


@pytest.mark.asyncio
async def test_upstream_guard_translates_invalid_translation_to_400() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _upstream_guard(
            "p", _invalid_translation_coro(), "stream", exc_handler=_stream_provider_error
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"] == "invalid_translation"
    assert excinfo.value.detail["message"] == "en not in [uk]"
    assert _samples_for("p") == []  # translation error skips recording


@pytest.mark.asyncio
async def test_upstream_guard_translates_translation_missing_to_404() -> None:
    """The translation_missing 404 path has no other unit coverage at
    the helper's surface — this test pins it."""
    with pytest.raises(HTTPException) as excinfo:
        await _upstream_guard(
            "p", _translation_missing_coro(), "stream", exc_handler=_stream_provider_error
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["error"] == "translation_missing"
    assert _samples_for("p") == []


@pytest.mark.asyncio
async def test_upstream_guard_falls_through_for_untranslated_provider_error() -> None:
    """ProviderError with a code the handler does NOT recognise must
    fall through to the canonical record+log+502 path — translation
    gating is per-code, not per-type."""
    with pytest.raises(HTTPException) as excinfo:
        await _upstream_guard(
            "p", _unknown_provider_error_coro(), "stream", exc_handler=_stream_provider_error
        )
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail["error"] == "upstream_unreachable"
    assert _samples_for("p") == [False]
