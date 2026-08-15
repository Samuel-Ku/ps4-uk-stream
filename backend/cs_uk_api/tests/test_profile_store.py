"""Tests for the viewer profile store (Arch T11, spec #309).

The store is the ONE seam for the facade's single-user viewer state:
``install`` is the only write path (playback reports, tests and
agent/LLM setup all go through it — no direct dict mutation), ``get``
answers the active profile with a cold-store default (callers never
branch on cold-vs-warm), and ``warm`` is the materialize primitive the
round-2 persistence (spec #323) will hook.

Seams under test:

  - ``get()`` on a cold store returns the empty default profile.
  - ``install()`` atomically replaces the active profile and returns it.
  - The content profile (a playback report through the router's
    ``_record_playback``) and an agent-installed profile land in the
    SAME store — both visible from one ``get()``.
  - ``warm()`` returns the current profile (cold → default), never None.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cs_uk_api.config import SETTINGS
from cs_uk_api.jellyfin.router import _record_playback
from cs_uk_api.profile_store import Profile, ProfileStore, profile_store


@pytest.fixture(autouse=True)
def _isolate() -> None:
    """Reset the module singleton through the seam (never mutate it)."""
    profile_store.install(Profile())
    yield
    profile_store.install(Profile())


# ----------------------------------------------------------------------
# get / install — the typed seam
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_cold_store_returns_empty_default() -> None:
    """A store with no installed profile answers the empty profile —
    callers never branch on cold-vs-warm."""
    store = ProfileStore(SETTINGS)
    assert store.get() == Profile()
    assert store.get().played == {}


@pytest.mark.unit
def test_install_round_trips_through_get() -> None:
    """install() sets the active profile; get() returns exactly it."""
    installed = Profile(played={"p1:s1e1": 600_000_000, "g2:x": 1_500_000_000})
    assert profile_store.install(installed) is installed
    assert profile_store.get() == installed
    assert dict(profile_store.get().played) == {
        "p1:s1e1": 600_000_000,
        "g2:x": 1_500_000_000,
    }


@pytest.mark.unit
def test_install_replaces_atomically() -> None:
    """A later install fully replaces the earlier profile (no merge)."""
    profile_store.install(Profile(played={"a": 1}))
    profile_store.install(Profile(played={"b": 2}))
    assert dict(profile_store.get().played) == {"b": 2}


@pytest.mark.unit
def test_profile_is_immutable_value() -> None:
    """A Profile cannot be rewritten after construction (frozen), so
    the only way to change the active profile is install() — no
    half-written profile is ever observable."""
    with pytest.raises(AttributeError):
        Profile(played={"a": 1}).played = {}  # type: ignore[misc]


# ----------------------------------------------------------------------
# warm — the materialize primitive
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_warm_on_cold_store_returns_default() -> None:
    """warm() on a cold store returns the empty profile, not None."""
    assert ProfileStore(SETTINGS).warm() == Profile()


@pytest.mark.unit
def test_warm_returns_active_profile() -> None:
    """warm() materializes/returns the active profile unchanged — the
    hook where round-2 persistence and the catalog-warm pipeline plug
    in without callers branching on cold-vs-warm."""
    installed = Profile(played={"p1:s1e1": 5})
    profile_store.install(installed)
    assert profile_store.warm() is installed


# ----------------------------------------------------------------------
# one store — content profile and agent profile share the seam
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_playback_report_and_install_share_the_store() -> None:
    """The content profile (a playback report) and an agent-installed
    profile land in the SAME store: both writes go through install(), a
    single get() observes the union."""
    _record_playback("p1:s1e1", 600_000_000)
    profile_store.install(Profile(played={**profile_store.get().played, "agent-key": 1}))
    assert dict(profile_store.get().played) == {
        "p1:s1e1": 600_000_000,
        "agent-key": 1,
    }


@pytest.mark.unit
def test_playback_report_ignores_zero_and_replaces_position() -> None:
    """The report policy (zero ticks ignored, last positive report wins)
    is preserved through the seam."""
    _record_playback("p1:s1e1", 0)
    assert profile_store.get().played == {}
    _record_playback("p1:s1e1", 100)
    _record_playback("p1:s1e1", 200)
    assert profile_store.get().played == {"p1:s1e1": 200}


# ----------------------------------------------------------------------
# round-2 persistence (spec #323, Store T1 #324) — thin adapter over the
# shared VersionedFileStore
# ----------------------------------------------------------------------


def _persisted_settings(tmp_path: Path):
    return replace(SETTINGS, profile_file=str(tmp_path / "profile.json"))


@pytest.mark.unit
def test_persistence_round_trip_restores_on_cold_start(tmp_path: Path) -> None:
    """install() persists; a fresh store over the same file (a process
    restart) restores the profile on construction."""
    first = ProfileStore(_persisted_settings(tmp_path))
    installed = Profile(played={"p1:s1e1": 600_000_000, "g2:x": 1_500_000_000})
    first.install(installed)
    second = ProfileStore(_persisted_settings(tmp_path))
    assert second.get() == installed
    assert second.warm() == installed


@pytest.mark.unit
def test_persistence_writes_versioned_envelope(tmp_path: Path) -> None:
    """The file carries the version token + adapter data (ADR-0003's
    obligation for persisted domain values)."""
    store = ProfileStore(_persisted_settings(tmp_path))
    store.install(Profile(played={"p1:s1e1": 5}))
    doc = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert doc == {"version": 1, "data": {"played": {"p1:s1e1": 5}}}


@pytest.mark.unit
def test_persistence_corrupt_file_degrades_to_empty_profile(tmp_path: Path) -> None:
    """A corrupt file never crashes the store: the ladder answers the
    empty default (logged), matching cold-store behaviour."""
    (tmp_path / "profile.json").write_text("{corrupt", encoding="utf-8")
    store = ProfileStore(_persisted_settings(tmp_path))
    assert store.get() == Profile()
    assert store.warm() == Profile()


@pytest.mark.unit
def test_persistence_unknown_version_degrades_to_empty_profile(tmp_path: Path) -> None:
    """An unknown version token (a shape from a future release) degrades
    to empty instead of mis-reading the payload."""
    (tmp_path / "profile.json").write_text(
        json.dumps({"version": 99, "data": {"played": {"p1:s1e1": 5}}}),
        encoding="utf-8",
    )
    assert ProfileStore(_persisted_settings(tmp_path)).get() == Profile()


@pytest.mark.unit
def test_no_persistence_when_path_unset(tmp_path: Path) -> None:
    """Default (no profile_file) keeps round-1 in-memory behaviour —
    install() writes nothing to disk."""
    store = ProfileStore(SETTINGS)
    store.install(Profile(played={"p1:s1e1": 1}))
    assert not (tmp_path / "profile.json").exists()
    assert ProfileStore(SETTINGS).get() == Profile()
