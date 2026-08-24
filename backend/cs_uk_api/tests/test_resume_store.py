"""Disk-backed resume store (ticket #248, spec #247; spec #363).

Tests the record/read pair and the persistence semantics the spec names
as the seam: positions survive a restart (a fresh store over the same
file keeps entries), a version-mismatched or corrupt file degrades to an
empty resume (never a crash), a Stopped report is flushed immediately
(no further write needed for a fresh store to see it), and the lifespan
flush persists debounced heartbeats.

Per spec #247's testing decisions these are wire/behaviour tests through
the store's record/read pair — file internals, locking and write timing
are never asserted.

Spec #363 moves the file onto the shared ``VersionedFileStore`` envelope
(``{"version": 1, "data": {items, queries, finished}}``); the legacy
bare ``{"v": n}`` shape degrades to a fresh empty resume with a warning
(the ADR-0003 remedy — pinned explicitly below).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import _catalog_state as catalog_state
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
    behind (atomic temp+rename, acceptance criterion 5). Spec #363: the
    file carries the shared outer envelope."""
    path = tmp_path / "playback.json"
    store = _fresh(str(path))
    store.record("g2:abc", 100, runtime_ticks=200)
    store.flush()
    assert os.path.exists(str(path))
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert doc["data"]["items"]["g2:abc"]["position_ticks"] == 100
    assert doc["data"]["items"]["g2:abc"]["runtime_ticks"] == 200


def test_store_flush_persists_debounced_record(tmp_path) -> None:
    """A debounced (Progress-heartbeat) record is persisted by the
    shutdown flush (acceptance criterion 4)."""

    async def scenario() -> dict[str, int]:
        store = _fresh(str(tmp_path / "playback.json"))
        store.record("g2:abc", 100)  # debounced — flushed only by flush()
        store.flush()
        return _fresh(str(tmp_path / "playback.json")).positions()

    assert asyncio.run(scenario()) == {"g2:abc": 100}


# ------------------------------------------------------------ T2 (#249)


def _clock(start: float = 1000.0) -> tuple[dict[str, float], callable[[], float]]:
    """Deterministic fake clock: each call advances by 1.0."""
    state = {"t": start}

    def now() -> float:
        state["t"] += 1.0
        return state["t"]

    return state, now


def test_store_finished_at_threshold_dropped(tmp_path) -> None:
    """#249: a report at >=95% of the runtime removes the item from the
    record/read pair, and the drop persists across a restart."""
    _, now = _clock()
    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    store.record("e1", 950, runtime_ticks=1000)
    store.flush()
    assert store.positions() == {}
    assert _fresh(str(tmp_path / "playback.json")).positions() == {}


def test_store_below_threshold_kept(tmp_path) -> None:
    """#249: 94.9% is not finished — the entry stays."""
    _, now = _clock()
    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    store.record("e1", 949, runtime_ticks=1000)
    assert store.positions() == {"e1": 949}


def test_store_without_runtime_never_finished(tmp_path) -> None:
    """#249: an item whose runtime is unknown is never auto-finished."""
    _, now = _clock()
    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    store.record("e1", 10**12)
    assert store.positions() == {"e1": 10**12}


def test_store_finished_uses_stored_runtime(tmp_path) -> None:
    """#249: a later report without a runtime still finishes against the
    runtime an earlier report stored."""
    _, now = _clock()
    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    store.record("e1", 100, runtime_ticks=200)
    store.record("e1", 195)  # >= 95% of the stored 200, no runtime on the report
    assert store.positions() == {}


def test_store_cap_evicts_least_recently_updated(tmp_path) -> None:
    """#249: the 51st distinct item evicts the least-recently-updated
    entry (LRU-50)."""
    _, now = _clock()
    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    for i in range(51):
        store.record(f"item{i}", 1000 + i)
    positions = store.positions()
    assert len(positions) == 50
    assert "item0" not in positions
    assert "item50" in positions


def test_store_recent_most_recent_first_capped(tmp_path) -> None:
    """#249: ``recent(limit)`` returns the most recently updated items
    first, capped at ``limit``."""
    _, now = _clock()
    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    store.record("a", 10)
    store.record("b", 20)
    store.record("c", 30)
    assert list(store.recent(2).keys()) == ["c", "b"]
    assert list(store.recent(10).keys()) == ["c", "b", "a"]


# ------------------------------------------------ legacy "v" files (#363)


def test_legacy_v_file_degrades_to_fresh_empty(tmp_path, caplog) -> None:
    """Spec #363 / ADR-0003 remedy: the pre-envelope ``{"v": n}``
    playback.json is NOT backward-read — it degrades to a fresh empty
    resume with a logged warning; startup never crashes. The one-time
    shelf loss re-accumulates from continued viewing."""
    path = tmp_path / "playback.json"
    legacy_v2 = {
        "v": 2,
        "items": {"g2:old": {"position_ticks": 123, "updated_at": 1.0}},
        "queries": ["старе"],
        "finished": {"g2:done": 99.0},
    }
    path.write_text(json.dumps(legacy_v2), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cs_uk_api.resume"):
        store = _fresh(str(path))
    assert store.positions() == {}
    assert store.recent_queries() == []
    assert store.history() == []
    assert "resume" in caplog.text.lower()
    # The next write replaces the legacy file with the new envelope.
    store.record("g2:new", 100)
    store.flush()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert doc["data"]["items"]["g2:new"]["position_ticks"] == 100


async def test_store_progress_heartbeat_is_debounced(tmp_path) -> None:
    """Inside a running loop a Progress heartbeat defers the write past
    the idle window (event-loop-native debounce), yet still lands on
    disk without an explicit flush."""
    path = str(tmp_path / "playback.json")
    store = ResumeStore(path, debounce_s=0.05)
    try:
        store.record("g2:abc", 100)
        assert not os.path.exists(path)  # window not elapsed
        await asyncio.sleep(0.25)
        assert _fresh(path).positions() == {"g2:abc": 100}
    finally:
        store.flush()


# ------------------------------------------------------------ queries (#252)


def test_store_queries_round_trip(tmp_path) -> None:
    """#252: recorded queries persist — newest first — across a fresh
    store over the same file (restart)."""
    path = str(tmp_path / "playback.json")
    first = _fresh(path)
    first.record_query("Дюна")
    first.record_query("Наруто")
    first.flush()
    second = _fresh(path)
    assert second.recent_queries() == ["Наруто", "Дюна"]


def test_store_queries_dedup_and_cap(tmp_path) -> None:
    """#252: queries are deduped (a repeat moves to the front) and the
    list is bounded at 50, newest first."""
    store = _fresh(str(tmp_path / "playback.json"))
    for i in range(55):
        store.record_query(f"query{i}")
    qs = store.recent_queries()
    assert len(qs) == 50
    assert qs[0] == "query54"
    # a repeat moves to the front without growing the list
    store.record_query("query10")
    qs = store.recent_queries()
    assert qs[0] == "query10"
    assert len(qs) == 50


def test_store_blank_query_ignored(tmp_path) -> None:
    """#252: a blank query is not recorded."""
    store = _fresh(str(tmp_path / "playback.json"))
    store.record_query("   ")
    assert store.recent_queries() == []


def test_store_queries_flush_with_state(tmp_path) -> None:
    """#252: items and queries share one atomic file — a flush persists
    both (spec #363: inside the shared outer envelope)."""
    path = str(tmp_path / "playback.json")
    store = _fresh(path)
    store.record("g2:abc", 100)
    store.record_query("Дюна")
    store.flush()
    doc = json.loads((tmp_path / "playback.json").read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert doc["data"]["items"]["g2:abc"]["position_ticks"] == 100
    assert doc["data"]["queries"] == ["Дюна"]


# ------------------------------------------------------------ T3 (#250)


def test_store_entries_carry_runtime(tmp_path) -> None:
    """#250: the entry reads expose (position, runtime) so the routes
    can put RunTimeTicks on the wire; unknown runtime is None."""
    _, now = _clock()
    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    store.record("e1", 100, runtime_ticks=200)
    store.record("e2", 300)  # no runtime
    assert store.positions_entries() == {"e2": (300, None), "e1": (100, 200)}
    assert store.recent_entries(10) == {"e2": (300, None), "e1": (100, 200)}
    assert store.recent_entries(1) == {"e2": (300, None)}


def test_stopped_report_flushed_immediately(client: TestClient, tmp_path, monkeypatch) -> None:
    """Wire-level: a Stopped report writes the state file right away — a
    fresh store over the same path (a restarted process) sees the item
    with no further write (acceptance criterion 1, flush criterion)."""
    path = str(tmp_path / "playback.json")
    original = catalog_state._resume_store
    catalog_state.install_resume_store(_fresh(path))
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
        catalog_state.install_resume_store(original)


# -------------------------------------------------- finished history (#272)


def test_store_finished_lands_in_history(tmp_path) -> None:
    """#272: a report at >=95% of the runtime leaves the shelves BUT is
    recorded in the finished history — the «Нещодавно переглянуто»
    signal the resume shelf deliberately drops."""
    store = _fresh(str(tmp_path / "playback.json"))
    store.record("g2:abc", 950, runtime_ticks=1000)
    assert store.positions() == {}
    assert "g2:abc" in store.history()


def test_store_history_merges_active_and_finished_most_recent_first(
    tmp_path,
) -> None:
    """#272: history() is the UNION of the active resume entries and the
    finished history, ordered by recency — the row lists everything the
    viewer recently watched, finished titles included."""
    state = {"t": 100.0}

    def now() -> float:
        state["t"] += 1.0
        return state["t"]

    store = ResumeStore(str(tmp_path / "playback.json"), now=now)
    store.record("e1", 100)  # active, t=101
    store.record("e2", 950, runtime_ticks=1000)  # finished, t=102
    store.record("e3", 200)  # active, t=103
    assert store.history() == ["e3", "e2", "e1"]


def test_store_history_round_trips_across_restart(tmp_path) -> None:
    """#272: the finished history persists — a fresh store over the same
    file (restart) still reports it."""
    path = str(tmp_path / "playback.json")
    first = _fresh(path)
    first.record("g2:abc", 950, runtime_ticks=1000)
    first.flush()
    second = _fresh(path)
    assert "g2:abc" in second.history()


def test_store_history_capped_at_limit(tmp_path) -> None:
    """#272: history() honors the caller's limit (the row asks for 20)."""
    store = _fresh(str(tmp_path / "playback.json"))
    for i in range(25):
        store.record(f"g2:{i}", 100 + i)
    assert len(store.history(limit=20)) == 20
    assert len(store.history()) == 20


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)
