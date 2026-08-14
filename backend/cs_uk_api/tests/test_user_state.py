"""Persisted user-state store (ticket #258, spec #257).

Tests the favorites/played record/read pair and the persistence
semantics the spec names as the seam: marks survive a restart (a fresh
store over the same file keeps them), a version-mismatched or corrupt
file degrades to empty state (never a crash), a toggle flips the mark,
and writes are atomic (full-snapshot temp+rename). File internals,
locking and write timing are never asserted.
"""

from __future__ import annotations

import json

from cs_uk_api.config import _load_user_state_path
from cs_uk_api.user_state import USER_STATE_VERSION, UserStateStore


def _fresh(path: str) -> UserStateStore:
    return UserStateStore(path)


def test_store_toggle_favorite_reads_back(tmp_path) -> None:
    store = _fresh(str(tmp_path / "user-state.json"))
    store.set_favorite("g2:abc", True)
    assert store.is_favorite("g2:abc") is True
    assert store.is_favorite("g2:other") is False
    store.set_favorite("g2:abc", False)
    assert store.is_favorite("g2:abc") is False


def test_store_played_reads_back(tmp_path) -> None:
    store = _fresh(str(tmp_path / "user-state.json"))
    store.set_played("p1:serial-1:s1e1", True)
    assert store.is_played("p1:serial-1:s1e1") is True
    assert store.is_played("g2:abc") is False
    store.set_played("p1:serial-1:s1e1", False)
    assert store.is_played("p1:serial-1:s1e1") is False


def test_store_favorites_and_played_independent(tmp_path) -> None:
    """Favoriting does not touch the played list and vice versa."""
    store = _fresh(str(tmp_path / "user-state.json"))
    store.set_favorite("g2:abc", True)
    store.set_played("g2:abc", True)
    store.set_played("g2:abc", False)
    assert store.is_favorite("g2:abc") is True  # untouched by the played toggle


def test_store_restart_keeps_marks(tmp_path) -> None:
    """Restart simulation: a fresh store over the same file keeps the
    favorite + played marks (acceptance criterion: survive restarts)."""
    path = str(tmp_path / "user-state.json")
    first = _fresh(path)
    first.set_favorite("g2:abc", True)
    first.set_played("p1:serial-1:s1e1", True)
    first.flush()
    second = _fresh(path)
    assert second.is_favorite("g2:abc") is True
    assert second.is_played("p1:serial-1:s1e1") is True
    assert second.is_played("g2:abc") is False


def test_store_ignores_version_mismatch(tmp_path, caplog) -> None:
    """A file carrying a different version token is ignored — empty
    state, warning logged, no crash."""
    path = tmp_path / "user-state.json"
    path.write_text(
        json.dumps({"v": 999, "favorites": ["g2:abc"], "played": ["g2:abc"]}),
        encoding="utf-8",
    )
    store = _fresh(str(path))
    assert store.is_favorite("g2:abc") is False
    assert store.is_played("g2:abc") is False


def test_store_corrupt_file_yields_empty(tmp_path, caplog) -> None:
    """Unparseable JSON degrades to empty state — never a crash."""
    path = tmp_path / "user-state.json"
    path.write_text("{not json", encoding="utf-8")
    store = _fresh(str(path))
    assert store.is_favorite("g2:abc") is False
    assert store.is_played("g2:abc") is False


def test_store_missing_file_is_clean(tmp_path) -> None:
    store = _fresh(str(tmp_path / "user-state.json"))
    assert store.favorites() == []
    assert store.played() == []


def test_store_atomic_write_round_trips(tmp_path) -> None:
    """The written file is the full versioned snapshot — a fresh store
    reads it back without a flush (the write happened synchronously)."""
    path = str(tmp_path / "user-state.json")
    store = _fresh(path)
    store.set_favorite("g2:abc", True)
    store.set_played("g2:abc", True)
    with open(path, encoding="utf-8") as fh:
        raw = json.loads(fh.read())
    assert raw["v"] == USER_STATE_VERSION
    assert set(raw["favorites"]) == {"g2:abc"}
    assert set(raw["played"]) == {"g2:abc"}


def test_store_bounded_lists_dedup(tmp_path) -> None:
    """Repeated marks never duplicate; the lists stay bounded."""
    store = _fresh(str(tmp_path / "user-state.json"))
    for _ in range(3):
        store.set_favorite("g2:abc", True)
    for i in range(500):
        store.set_favorite(f"g2:{i}", True)
    favs = store.favorites()
    assert favs.count("g2:abc") == 1
    assert len(favs) <= 256


def test_env_knob_resolves_path(monkeypatch, tmp_path) -> None:
    """#258 AC6: the file location is configurable via the env knob —
    a custom path is honored, and an explicit empty string disables the
    disk layer (memory-only)."""
    custom = str(tmp_path / "custom" / "state.json")
    monkeypatch.setenv("CS_UK_USER_STATE_PATH", custom)
    monkeypatch.setenv("CS_UK_RESUME_PATH", "")
    assert _load_user_state_path() == custom

    monkeypatch.setenv("CS_UK_USER_STATE_PATH", "")
    assert _load_user_state_path() is None

    # Unset → default next to the poster disk cache parent.
    monkeypatch.delenv("CS_UK_USER_STATE_PATH")
    monkeypatch.setenv("CS_UK_POSTER_CACHE_DIR", str(tmp_path / "posters"))
    resolved = _load_user_state_path()
    assert resolved is not None and resolved.endswith("user-state.json")


def test_env_knob_unset_defaults_next_to_resume(monkeypatch, tmp_path) -> None:
    """#258 AC6: with the knob unset the file lands next to the resume
    file (which itself defaults next to the poster disk cache)."""
    monkeypatch.delenv("CS_UK_USER_STATE_PATH")
    monkeypatch.setenv("CS_UK_RESUME_PATH", str(tmp_path / "state" / "playback.json"))
    assert _load_user_state_path() == str(tmp_path / "state" / "user-state.json")
