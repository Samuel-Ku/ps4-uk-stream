"""Disk-backed resume store (ticket #248, spec #247).

Tests the record/read pair and the persistence semantics the spec names
as the seam: positions survive a restart (a fresh store over the same
file keeps entries), a version-mismatched or corrupt file degrades to an
empty resume (never a crash), a Stopped report is flushed immediately
(no further write needed for a fresh store to see it), and the lifespan
flush persists debounced heartbeats.

Per spec #247's testing decisions these are wire/behaviour tests through
the store's record/read pair — file internals, locking and write timing
are never asserted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import catalog_state
from cs_uk_api.config import SETTINGS
from cs_uk_api.resume_store import ResumeStore

TOKEN = SETTINGS.jellyfin_token


def _fresh(path: str) -> ResumeStore:
    return ResumeStore(path)


def test_store_record_read_position_and_runtime(tmp_path) -> None:
    """record -> read shows the position; the runtime rides along in the
    entry so a later tranche can mark finished items (#247 format)."""
    store = _fresh(str(tmp_path / "playback.json"))
    store.record("g2:abc", 1_500_000_000, runtime_ticks=2_000_000_000)
    assert store.positions() == {"g2:abc": 1_500_000_000}
    assert store.entries()["g2:abc"]["runtime_ticks"] == 2_000_000_000


def test_store_restart_keeps_entries(tmp_path) -> None:
    """Restart simulation: a fresh store over the same file keeps the
    recorded position (acceptance criterion 1)."""
    path = str(tmp_path / "playback.json")
    first = _fresh(path)
    first.record("g2:abc", 1_500_000_000)
    first.flush()
    second = _fresh(path)
    assert second.positions() == {"g2:abc": 1_500_000_000}


def test_store_ignores_version_mismatch(tmp_path, caplog) -> None:
    """A file carrying a different version token is ignored — empty
    resume, warning logged, no crash (acceptance criterion 2)."""
    path = tmp_path / "playback.json"
    path.write_text(json.dumps({"v": 999, "items": {"g2:abc": {"position_ticks": 1}}}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cs_uk_api.resume"):
        store = _fresh(str(path))
    assert store.positions() == {}
    assert "version" in caplog.text.lower()


def test_store_corrupt_file_yields_empty(tmp_path, caplog) -> None:
    """An unparseable file yields an empty resume and the API keeps
    serving (acceptance criterion 3)."""
    path = tmp_path / "playback.json"
    path.write_text("{ not json !!!", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cs_uk_api.resume"):
        store = _fresh(str(path))
    assert store.positions() == {}


def test_store_missing_file_is_clean(tmp_path) -> None:
    """A fresh path starts empty and does not create a file on read."""
    path = str(tmp_path / "playback.json")
    store = _fresh(path)
    assert store.positions() == {}
    assert not os.path.exists(path)


def test_store_zero_position_not_recorded(tmp_path) -> None:
    """A just-started (0) report must not seed the shelf — and must not
    be persisted either."""
    path = str(tmp_path / "playback.json")
    store = _fresh(path)
    store.record("g2:abc", 0)
    store.flush()
    assert store.positions() == {}
    assert _fresh(path).positions() == {}


def test_store_atomic_write_round_trips(tmp_path) -> None:
    """The flushed file is valid versioned JSON and no temp file is left
    behind (atomic temp+rename, acceptance criterion 5)."""
    path = tmp_path / "playback.json"
    store = _fresh(str(path))
    store.record("g2:abc", 100, runtime_ticks=200)
    store.flush()
    assert os.path.exists(str(path))
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["v"] == 1
    assert data["items"]["g2:abc"]["position_ticks"] == 100
    assert data["items"]["g2:abc"]["runtime_ticks"] == 200


def test_store_flush_persists_debounced_record(tmp_path) -> None:
    """A debounced (Progress-heartbeat) record is persisted by the
    shutdown flush (acceptance criterion 4)."""

    async def scenario() -> dict[str, int]:
        store = _fresh(str(tmp_path / "playback.json"))
        store.record("g2:abc", 100)  # debounced — flushed only by flush()
        store.flush()
        return _fresh(str(tmp_path / "playback.json")).positions()

    assert asyncio.run(scenario()) == {"g2:abc": 100}


def test_stopped_report_flushed_immediately(client: TestClient, tmp_path, monkeypatch) -> None:
    """Wire-level: a Stopped report writes the state file right away — a
    fresh store over the same path (a restarted process) sees the item
    with no further write (acceptance criterion 1, flush criterion)."""
    path = str(tmp_path / "playback.json")
    original = catalog_state._resume_store
    catalog_state._resume_store = _fresh(path)
    try:
        r = client.post(
            "/Sessions/Playing/Stopped",
            json={"ItemId": "g2:abc", "PositionTicks": 1_500_000_000, "RunTimeTicks": 2_000_000_000},
            headers={"X-Emby-Token": TOKEN},
        )
        assert r.status_code == 204
        restarted = _fresh(path)
        assert restarted.positions() == {"g2:abc": 1_500_000_000}
        assert restarted.entries()["g2:abc"]["runtime_ticks"] == 2_000_000_000
    finally:
        catalog_state._resume_store = original


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)
