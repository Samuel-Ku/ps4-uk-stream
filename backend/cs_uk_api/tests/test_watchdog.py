"""Tests for the wedged-client watchdog (ticket #215).

Seams under test:
- unit: ``WedgedClientWatchdog.all_relevant_down`` — all non-marker
  providers report ``down`` simultaneously (never a legit steady state).
- unit: ``should_reset`` cooldown — a reset is rate-limited so a
  genuinely-down network can't churn a fresh client every tick.
- unit: ``check_and_reset`` closes the shared client and counts resets.
- unit: after ``close_client``, ``get_client()`` hands out a NEW client
  (the recovery mechanism: a fresh connection pool).
- API:  ``GET /api/health`` exposes per-provider status, all-down, and
  watchdog state.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import http_client, watchdog
from cs_uk_api.main import app
from cs_uk_api.models import STATUS_DOWN, STATUS_OK
from cs_uk_api.watchdog import WedgedClientWatchdog

client = TestClient(app)


class FakeTracker:
    """Minimal HealthTracker stand-in: status + startup marker."""

    def __init__(self, statuses: dict[str, str], markers: set[str] | None = None) -> None:
        self._statuses = statuses
        self._markers = markers or set()

    def status(self, provider_id: str) -> str:
        return self._statuses.get(provider_id, STATUS_OK)

    def startup_marker(self, provider_id: str) -> str | None:
        return provider_id if provider_id in self._markers else None


def make_providers(ids: list[str]) -> dict[str, object]:
    return {pid: object() for pid in ids}


# ---------------------------------------------------------------------------
# all_relevant_down (unit)
# ---------------------------------------------------------------------------

def test_all_providers_down_detected() -> None:
    tracker = FakeTracker({"a": STATUS_DOWN, "b": STATUS_DOWN, "c": STATUS_DOWN})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a", "b", "c"]))
    assert w.all_relevant_down() is True


def test_any_provider_up_means_not_all_down() -> None:
    tracker = FakeTracker({"a": STATUS_DOWN, "b": STATUS_OK, "c": STATUS_DOWN})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a", "b", "c"]))
    assert w.all_relevant_down() is False


def test_insufficient_data_is_not_down() -> None:
    # A provider with no samples reports ``ok`` (insufficient data) — the
    # watchdog must not fire on a cold tracker at startup.
    tracker = FakeTracker({})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a", "b"]))
    assert w.all_relevant_down() is False


def test_startup_marker_provider_does_not_veto() -> None:
    # uakino is pinned ``down`` at startup when Chromium is missing — an
    # expected-down provider must not make "all relevant down" false when
    # every OTHER provider is wedged, and must not make it true on its own.
    tracker = FakeTracker(
        {"a": STATUS_DOWN, "b": STATUS_DOWN, "uakino": STATUS_DOWN},
        markers={"uakino"},
    )
    w = WedgedClientWatchdog(
        tracker=tracker,
        providers=make_providers(["a", "b", "uakino"]),
    )
    assert w.all_relevant_down() is True

    # Marker alone (everything else healthy) is NOT the wedge.
    tracker = FakeTracker(
        {"a": STATUS_OK, "b": STATUS_OK, "uakino": STATUS_DOWN},
        markers={"uakino"},
    )
    w = WedgedClientWatchdog(
        tracker=tracker,
        providers=make_providers(["a", "b", "uakino"]),
    )
    assert w.all_relevant_down() is False


def test_no_relevant_providers_is_not_down() -> None:
    tracker = FakeTracker({}, markers={"a", "b"})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a", "b"]))
    assert w.all_relevant_down() is False


# ---------------------------------------------------------------------------
# should_reset / cooldown (unit)
# ---------------------------------------------------------------------------

def test_should_reset_when_all_down_and_no_recent_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 1000.0)
    tracker = FakeTracker({"a": STATUS_DOWN})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a"]), cooldown_s=300)
    assert w.should_reset() is True


def test_cooldown_blocks_repeated_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: now)
    tracker = FakeTracker({"a": STATUS_DOWN})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a"]), cooldown_s=300)
    assert w.should_reset() is True
    w._last_reset_at = now
    assert w.should_reset() is False
    now += 301
    assert w.should_reset() is True


def test_no_reset_when_not_all_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 1000.0)
    tracker = FakeTracker({"a": STATUS_OK})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a"]), cooldown_s=300)
    assert w.should_reset() is False


# ---------------------------------------------------------------------------
# check_and_reset (unit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_and_reset_closes_client_and_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 1000.0)
    closed = 0

    async def fake_close() -> None:
        nonlocal closed
        closed += 1

    monkeypatch.setattr(watchdog, "close_client", fake_close)
    tracker = FakeTracker({"a": STATUS_DOWN})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a"]))
    assert await w.check_and_reset() is True
    assert closed == 1
    assert w.reset_count == 1
    assert w.last_reset_at == 1000.0


@pytest.mark.asyncio
async def test_check_and_reset_respects_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: now)
    closed = 0

    async def fake_close() -> None:
        nonlocal closed
        closed += 1

    monkeypatch.setattr(watchdog, "close_client", fake_close)
    tracker = FakeTracker({"a": STATUS_DOWN})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a"]))
    assert await w.check_and_reset() is True
    # Still all-down but inside the cooldown window: no second reset.
    assert await w.check_and_reset() is False
    assert closed == 1
    assert w.reset_count == 1


@pytest.mark.asyncio
async def test_check_and_reset_noop_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = 0

    async def fake_close() -> None:
        nonlocal closed
        closed += 1

    monkeypatch.setattr(watchdog, "close_client", fake_close)
    tracker = FakeTracker({"a": STATUS_OK})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a"]))
    assert await w.check_and_reset() is False
    assert closed == 0


# ---------------------------------------------------------------------------
# client recreation (unit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_client_then_get_client_is_a_new_object() -> None:
    """Closing the shared client must detach the singleton so the next
    get_client() builds a fresh connection pool — the recovery mechanism
    behind #215."""
    await http_client.close_client()  # ensure clean slate
    first = http_client.get_client()
    assert first is not None
    await http_client.close_client()
    second = http_client.get_client()
    assert second is not first
    await http_client.close_client()  # restore clean slate for other tests


# ---------------------------------------------------------------------------
# GET /api/health (API seam)
# ---------------------------------------------------------------------------

def test_health_endpoint_exposes_status_and_watchdog() -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body
    assert "all_down" in body
    assert "watchdog" in body
    for status in body["providers"].values():
        assert status in ("ok", "degraded", "down", "warming")


def test_health_all_down_flag_reflects_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = FakeTracker({"a": STATUS_DOWN, "b": STATUS_DOWN})
    w = WedgedClientWatchdog(tracker=tracker, providers=make_providers(["a", "b"]))
    monkeypatch.setattr(watchdog, "WATCHDOG", w)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["all_down"] is True


def test_health_exposes_recommendation_profile_counts() -> None:
    """#253 AC5: the existing /api/health surface exposes profile-store
    counts (profiles / queries / watched) — debuggable without a new
    endpoint."""
    from cs_uk_api import catalog_state

    # Other test modules share the process-wide store — start from a
    # clean slate so the counts are exact.
    catalog_state.clear_playback()
    catalog_state._profiles = {"g2:a": object(), "g2:b": object()}  # type: ignore[assignment]
    catalog_state.record_search_query("Дюна")
    catalog_state.record_search_query("Наруто")
    catalog_state.record_playback("g2:a", 1_000_000_000)
    try:
        r = client.get("/api/health")
        assert r.status_code == 200
        rec = r.json()["recommendations"]
        assert rec["profiles"] == 2
        assert rec["queries"] == 2
        assert rec["watched"] == 1
    finally:
        catalog_state._profiles = {}
        catalog_state.clear_playback()
