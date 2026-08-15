"""Disk-backed home snapshot store (ticket #269, spec #267 T2).

The first home read after a backend restart used to pay a full provider
fan-out (17-21s, B1) before the facade could answer ``/UserViews`` —
past the client's request timeout. The store persists the last
successful home build to one versioned JSON file so a cold start
serves it at ANY age and heals in the background.

Seams under test (the same contract as the other persisted domain
objects, spec #247/#257):

  - ``SnapshotStore.save`` writes rows + the group resolution map to a
    single file, atomically (temp + rename), versioned.
  - ``SnapshotStore.load`` round-trips what was saved — both sides of
    the tuple — from a NEW store instance (the restart case).
  - A corrupt / truncated / version-mismatched file degrades to
    ``(None, None)`` with a warning — never a crash.
  - ``path=None`` (the test-suite default) is memory-only: save and
    load are no-ops.
  - A write into a missing directory creates it (first-run layout).
"""

from __future__ import annotations

import json

import pytest

import cs_uk_api.catalog_state._stores as stores_mod
import cs_uk_api.catalog_state.snapshot as snapshot_mod
from cs_uk_api.models import HomeResponse, HomeRow, SearchResult
from cs_uk_api.snapshot_store import SNAPSHOT_VERSION, SnapshotStore


def _row(title: str = "Фільми") -> HomeRow:
    return HomeRow(
        title=title,
        type="movie",
        items=[
            {
                "group_key": "g2:abc",
                "provider": "p1",
                "external_id": "1",
                "title": "Дюна",
                "year": 2021,
                "poster": "https://cdn.example/p.jpg",
                "form": "movie",
            }
        ],
    )


def _home() -> HomeResponse:
    return HomeResponse(rows=[_row()])


def _source_item() -> SearchResult:
    return SearchResult(
        id="p1:1",
        provider="p1",
        form="movie",
        title="Дюна",
        year=2021,
        url="https://p1.example/1",
    )


def _sources() -> dict[str, dict[str, SearchResult]]:
    return {"g2:abc": {"p1": _source_item()}}


def test_save_then_fresh_load_round_trips(tmp_path: pytest.TempPathFactory) -> None:
    """The restart case: save from one store, load from a NEW store over
    the same path — rows AND the sources map come back identical."""
    path = str(tmp_path / "snapshot.json")
    SnapshotStore(path).save(_home(), _sources())

    home, sources = SnapshotStore(path).load()
    assert home is not None
    assert [r.title for r in home.rows] == ["Фільми"]
    assert home.rows[0].items[0].group_key == "g2:abc"
    assert home.rows[0].items[0].title == "Дюна"
    assert sources is not None
    assert sources["g2:abc"]["p1"].title == "Дюна"
    assert sources["g2:abc"]["p1"].year == 2021


def test_load_missing_file_is_none(tmp_path: pytest.TempPathFactory) -> None:
    assert SnapshotStore(str(tmp_path / "absent.json")).load() == (None, None)


def test_corrupt_file_degrades_to_none(tmp_path: pytest.TempPathFactory) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{ this is not json", encoding="utf-8")
    home, sources = SnapshotStore(str(path)).load()
    assert home is None
    assert sources is None


def test_version_mismatch_degrades_to_none(tmp_path: pytest.TempPathFactory) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps({"v": SNAPSHOT_VERSION + 99, "rows": []}), encoding="utf-8"
    )
    home, sources = SnapshotStore(str(path)).load()
    assert home is None
    assert sources is None


def test_bad_rows_shape_degrades_to_none(tmp_path: pytest.TempPathFactory) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"v": SNAPSHOT_VERSION, "rows": "nope"}), encoding="utf-8")
    home, sources = SnapshotStore(str(path)).load()
    assert home is None
    assert sources is None


def test_partially_corrupt_sources_are_skipped(tmp_path: pytest.TempPathFactory) -> None:
    """A bad entry inside the sources map must not kill the whole load —
    the good source survives, the bad one is dropped."""
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "v": SNAPSHOT_VERSION,
                "rows": [_row().model_dump(mode="json")],
                "sources": {
                    "g2:abc": {
                        "p1": _source_item().model_dump(mode="json"),
                        "p2": "garbage",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    home, sources = SnapshotStore(str(path)).load()
    assert home is not None
    assert sources is not None
    assert set(sources["g2:abc"]) == {"p1"}


def test_memory_only_store_is_a_noop() -> None:
    store = SnapshotStore(None)
    store.save(_home(), _sources())
    assert store.load() == (None, None)


def test_save_creates_missing_parent_dir(tmp_path: pytest.TempPathFactory) -> None:
    path = tmp_path / "nested" / "dir" / "snapshot.json"
    SnapshotStore(str(path)).save(_home(), _sources())
    assert path.exists()


def test_saved_file_is_versioned_atomic_json(tmp_path: pytest.TempPathFactory) -> None:
    """The on-disk shape: one versioned JSON object with rows + sources,
    no leftover temp files after the write."""
    path = tmp_path / "snapshot.json"
    SnapshotStore(str(path)).save(_home(), _sources())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["v"] == SNAPSHOT_VERSION
    assert [r["title"] for r in payload["rows"]] == ["Фільми"]
    assert payload["sources"]["g2:abc"]["p1"]["title"] == "Дюна"
    assert list(tmp_path.glob("*.tmp")) == []


def test_cold_start_serves_persisted_snapshot_without_fanout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """The whole point of #269 (wire-level): after a "restart" (fresh
    store over the persisted file, empty home cache), ``load_home``
    answers from the file at ANY age and the provider fan-out never
    runs — a stubbed registry proves no ``browse()`` call happens.
    The background heal is stubbed out so the assertion sees the
    restored snapshot, not whatever the rebuild would overwrite.
    """
    import asyncio
    from dataclasses import replace

    import cs_uk_api.catalog_state as cs
    from cs_uk_api.config import SETTINGS

    path = tmp_path / "home-snapshot.json"
    SnapshotStore(str(path)).save(_home(), _sources())

    # The snapshot store re-reads ``_config.SETTINGS.snapshot_path`` at
    # ``clear_snapshot_store`` time (spec #309 T5: the store lives in
    # the ``_stores`` internal module).
    original_store = cs._snapshot_store()
    monkeypatch.setattr(
        stores_mod._config, "SETTINGS", replace(SETTINGS, snapshot_path=str(path))
    )
    try:
        cs.clear_snapshot_store()
        cs.home_cache.clear()
        cs.sources_cache.clear()
        cs.PROVIDERS.clear()
        # The heal rebuild would run against the empty registry; stub it so
        # the test isolates the restore path.
        async def _noop_heal() -> None:
            return None

        monkeypatch.setattr(snapshot_mod, "_build_home", _noop_heal)

        home = asyncio.run(cs.load_home())
        assert [r.title for r in home.rows] == ["Фільми"]
        assert home.rows[0].items[0].group_key == "g2:abc"
        # No provider was registered — the persisted snapshot answered.
        assert cs.PROVIDERS == {}

        # Group resolution for a persisted row works without any provider:
        # the sources map was restored with the snapshot.
        per_provider = cs.resolve_group("g2:abc")
        assert per_provider is not None and per_provider["p1"].title == "Дюна"
    finally:
        # Restore the suite's memory-only store: leaking the temp-path
        # store would keep serving the persisted file to every later
        # cold ``load_home`` in the process (test isolation).
        cs.install_snapshot_store(original_store)
