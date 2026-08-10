"""Tests for provider health tracking (issue #53, v3 spec §2.1/3.4).

Seams under test:
- unit: ``HealthTracker`` (windowed error rates, recovery, startup markers)
- API:  ``GET /api/providers`` exposing status/last_error_at, driven by
  real failures induced through ``/api/search``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import health
from cs_uk_api.health import HealthTracker
from cs_uk_api.main import app
from cs_uk_api.models import SearchResult
from cs_uk_api.providers import PROVIDERS

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_tracker():
    health.TRACKER.reset()
    yield
    health.TRACKER.reset()


def make_tracker() -> HealthTracker:
    return HealthTracker(window=5, min_samples=3, degraded_at=0.4, down_at=0.8)


# ---------------------------------------------------------------------------
# HealthTracker (unit)
# ---------------------------------------------------------------------------

def test_unknown_provider_is_ok() -> None:
    t = make_tracker()
    assert t.status("ghost") == "ok"
    assert t.last_error_at("ghost") is None


def test_insufficient_samples_ids_ok() -> None:
    t = make_tracker()
    for _ in range(2):  # below min_samples=3
        t.record("p", ok=False)
    assert t.status("p") == "ok"


def test_all_failures_down() -> None:
    t = make_tracker()
    for _ in range(3):
        t.record("p", ok=False)
    assert t.status("p") == "down"


def test_mixed_failures_degraded() -> None:
    t = make_tracker()
    for _ in range(2):
        t.record("p", ok=False)
    t.record("p", ok=True)
    assert t.status("p") == "degraded"  # error rate 2/3 ≈ 0.67


def test_recovery_from_down_to_ok() -> None:
    t = make_tracker()
    for _ in range(5):
        t.record("p", ok=False)
    assert t.status("p") == "down"
    for _ in range(5):  # window becomes all-success
        t.record("p", ok=True)
    assert t.status("p") == "ok"


def test_last_error_at_tracks_failures_only() -> None:
    t = make_tracker()
    t.record("p", ok=True)
    assert t.last_error_at("p") is None
    t.record("p", ok=False)
    first = t.last_error_at("p")
    assert first is not None
    t.record("p", ok=True)
    assert t.last_error_at("p") == first


def test_startup_marker_forces_down() -> None:
    t = make_tracker()
    t.mark_startup("uakino", "chromium_missing")
    assert t.status("uakino") == "down"
    # ...and stays down until the host recovers (reset/restart).
    t.record("uakino", ok=True)
    assert t.status("uakino") == "down"


def test_reset_clears_state() -> None:
    t = make_tracker()
    t.record("p", ok=False)
    t.mark_startup("p", "reason")
    t.reset()
    assert t.status("p") == "ok"
    assert t.last_error_at("p") is None


# ---------------------------------------------------------------------------
# GET /api/providers (API seam)
# ---------------------------------------------------------------------------

def test_providers_endpoint_exposes_health_fields() -> None:
    r = client.get("/api/providers")
    assert r.status_code == 200
    for p in r.json():
        # ``warming`` is the transient pre-ready uakino state (issue #193).
        assert p["status"] in ("ok", "degraded", "down", "warming")
        assert "last_error_at" in p


def test_search_failures_flip_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(query, http):
        raise RuntimeError("site 522")

    monkeypatch.setattr(PROVIDERS["eneyida"], "search", boom)
    for i in range(5):
        r = client.get(f"/api/search?q=hurl{i}&provider=eneyida")
        assert r.status_code == 200  # a failing provider returns [] per contract

    p = {x["id"]: x for x in client.get("/api/providers").json()}
    assert p["eneyida"]["status"] == "down"
    assert p["eneyida"]["last_error_at"] is not None


def test_successful_search_recovers_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(query, http):
        raise RuntimeError("site 522")

    async def fine(query, http):
        return [SearchResult(id="eneyida:1", provider="eneyida", form="movie",
                             title="T", url="https://eneyida.example/1")]

    monkeypatch.setattr(PROVIDERS["eneyida"], "search", boom)
    for i in range(5):
        client.get(f"/api/search?q=bad{i}&provider=eneyida")
    p = {x["id"]: x for x in client.get("/api/providers").json()}
    assert p["eneyida"]["status"] == "down"

    monkeypatch.setattr(PROVIDERS["eneyida"], "search", fine)
    for i in range(20):  # refill the 20-sample window with successes
        client.get(f"/api/search?q=good{i}&provider=eneyida")
    p = {x["id"]: x for x in client.get("/api/providers").json()}
    assert p["eneyida"]["status"] == "ok"


def test_content_failure_counts_but_404s_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(external_id, http):
        raise RuntimeError("site 522")

    monkeypatch.setattr(PROVIDERS["eneyida"], "content", boom)
    for i in range(5):
        r = client.get(f"/api/content/eneyida:{i}")
        assert r.status_code == 502
    p = {x["id"]: x for x in client.get("/api/providers").json()}
    assert p["eneyida"]["status"] == "down"

    # 404s (unknown ids, blocked countries) are client-side semantics, not
    # upstream failures — they must not move the needle.
    health.TRACKER.reset()
    for i in range(5):
        r = client.get(f"/api/content/ghost:{i}")
        assert r.status_code == 404
    assert health.TRACKER.status("ghost") == "ok"
    assert health.TRACKER.status("eneyida") == "ok"
