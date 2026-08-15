"""VersionedFileStore + DebouncedSave tests (spec #323, Store T1 #324).

Covers the corrupt-safe load ladder (missing / unreadable / corrupt JSON
/ version mismatch / shape-invalid), the atomic write, the wire envelope,
and the optional debounced-save wrapper.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cs_uk_api.versioned_store import DebouncedSave, VersionedFileStore, atomic_write_text

_PAYLOAD = {"played": {"p1:s1e1": 600_000_000}}


def _make_store(path: Path, versions: tuple[int, ...] = (1,)) -> VersionedFileStore:
    def encode(payload: object) -> object:
        return {"value": payload}

    def decode(data: object) -> object:
        if not isinstance(data, dict) or "value" not in data:
            raise TypeError("bad shape")
        return data["value"]

    return VersionedFileStore(
        path=str(path),
        supported_versions=versions,
        encode=encode,
        decode=decode,
    )


def _write(path: Path, doc: object) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


# ----------------------------------------------------------------------
# the load ladder — every bad case degrades to None, never raises
# ----------------------------------------------------------------------


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert _make_store(tmp_path / "nope.json").load() is None


def test_load_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    p = tmp_path / "corrupt.json"
    p.write_text("{not json!!", encoding="utf-8")
    assert _make_store(p).load() is None


def test_load_returns_none_on_unreadable_path(tmp_path: Path) -> None:
    # A directory where a file is expected: read_text raises OSError.
    d = tmp_path / "adir"
    d.mkdir()
    assert _make_store(d).load() is None


def test_load_returns_none_on_bad_envelope(tmp_path: Path) -> None:
    p = tmp_path / "env.json"
    _write(p, {"foo": 1})
    assert _make_store(p).load() is None


def test_load_returns_none_on_version_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "ver.json"
    _write(p, {"version": 99, "data": {"value": _PAYLOAD}})
    assert _make_store(p).load() is None


def test_load_returns_none_on_shape_invalid(tmp_path: Path) -> None:
    p = tmp_path / "shape.json"
    _write(p, {"version": 1, "data": {"wrong": 1}})
    assert _make_store(p).load() is None


def test_load_degrades_with_a_log_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = tmp_path / "log.json"
    _write(p, {"version": 9, "data": {}})
    with caplog.at_level("WARNING", logger="cs_uk_api.versioned_store"):
        assert _make_store(p).load() is None
    assert any("version" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# round-trip + wire envelope
# ----------------------------------------------------------------------


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "rt.json")
    store.save(_PAYLOAD)
    assert store.load() == _PAYLOAD


def test_save_writes_versioned_envelope(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "env.json", versions=(1, 2))
    store.save(_PAYLOAD)
    doc = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert doc == {"version": 2, "data": {"value": _PAYLOAD}}


def test_load_accepts_any_supported_version(tmp_path: Path) -> None:
    p = tmp_path / "old.json"
    _write(p, {"version": 1, "data": {"value": _PAYLOAD}})
    # v1 files stay readable after the store learns v2.
    assert _make_store(p, versions=(1, 2)).load() == _PAYLOAD


def test_save_overwrites_previous_payload(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "ow.json")
    store.save({"a": 1})
    store.save({"b": 2})
    assert store.load() == {"b": 2}


# ----------------------------------------------------------------------
# atomicity — never raises, never leaves a half-written file
# ----------------------------------------------------------------------


def test_save_is_atomic_no_leftover_tmp_files(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "atomic.json")
    store.save(_PAYLOAD)
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == []
    assert store.load() == _PAYLOAD


def test_save_never_raises_on_unwritable_location(tmp_path: Path) -> None:
    # Parent is a FILE, so mkdir/mkstemp fail with OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    store = _make_store(blocker / "child.json")
    store.save(_PAYLOAD)  # must not raise
    assert not (blocker / "child.json").exists()


def test_save_never_raises_on_encode_failure(tmp_path: Path) -> None:
    def bad_encode(payload: object) -> object:
        raise RuntimeError("boom")

    store = VersionedFileStore(
        path=str(tmp_path / "bad.json"),
        supported_versions=(1,),
        encode=bad_encode,  # type: ignore[arg-type]
        decode=lambda d: d,  # type: ignore[arg-type]
    )
    store.save(_PAYLOAD)  # must not raise
    assert not (tmp_path / "bad.json").exists()


# ----------------------------------------------------------------------
# atomic_write_text — the shared plain-file write primitive
# ----------------------------------------------------------------------


def test_atomic_write_text_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "report.md"
    atomic_write_text(str(p), "# report\n")
    assert p.read_text(encoding="utf-8") == "# report\n"


def test_atomic_write_text_leaves_no_tmp_files(tmp_path: Path) -> None:
    p = tmp_path / "report.md"
    atomic_write_text(str(p), "x")
    atomic_write_text(str(p), "y")
    leftovers = [f.name for f in tmp_path.iterdir() if ".tmp" in f.name]
    assert leftovers == []
    assert p.read_text(encoding="utf-8") == "y"


def test_atomic_write_text_never_raises_on_unwritable_location(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    atomic_write_text(str(blocker / "child.md"), "x")  # must not raise
    assert not (blocker / "child.md").exists()


# ----------------------------------------------------------------------
# DebouncedSave — the optional coalescing wrapper
# ----------------------------------------------------------------------


def test_debounce_flush_writes_latest_payload_now(tmp_path: Path) -> None:
    saver = DebouncedSave(_make_store(tmp_path / "d.json"), delay_s=60.0)
    try:
        saver.request({"a": 1})
        saver.request({"b": 2})  # coalesced over the first
        saver.flush()
        assert _make_store(tmp_path / "d.json").load() == {"b": 2}
    finally:
        saver.close()


def test_debounce_writes_after_idle_window(tmp_path: Path) -> None:
    saver = DebouncedSave(_make_store(tmp_path / "idle.json"), delay_s=0.05)
    try:
        saver.request({"a": 1})
        time.sleep(0.25)  # comfortably past the idle window
        assert _make_store(tmp_path / "idle.json").load() == {"a": 1}
    finally:
        saver.close()


def test_debounce_no_write_before_idle_window(tmp_path: Path) -> None:
    saver = DebouncedSave(_make_store(tmp_path / "early.json"), delay_s=60.0)
    try:
        saver.request({"a": 1})
        # Nothing written yet — the window has not elapsed.
        assert _make_store(tmp_path / "early.json").load() is None
    finally:
        saver.close()


def test_debounce_close_writes_pending_and_joins(tmp_path: Path) -> None:
    saver = DebouncedSave(_make_store(tmp_path / "c.json"), delay_s=60.0)
    saver.request({"a": 1})
    saver.close()
    assert not saver._thread.is_alive()
    assert _make_store(tmp_path / "c.json").load() == {"a": 1}
