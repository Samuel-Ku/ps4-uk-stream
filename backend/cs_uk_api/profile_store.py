"""Viewer profile store — one clean seam (Arch T11, spec #309).

The facade serves one fixed user (D4, ADR-0002), so the viewer state
that user's "profile" carries is the resume/played memory the Jellyfin
Resume and NextUp rails read (ticket #214). This module is the single
seam for that state:

  - ``Profile`` — the typed, immutable viewer profile. ``played`` maps
    a played item's wire id to its position ticks.
  - ``ProfileStore.install()`` — the ONE write path: every actor that
    changes the active profile — the playback-report route, tests, and
    agent/LLM-driven setup — goes through it. No direct dict mutation
    anywhere.
  - ``ProfileStore.get()`` — the active profile; a cold store answers
    the empty default, so callers never branch on cold-vs-warm.
  - ``ProfileStore.warm()`` — materialize/refresh the active profile and
    return it. Today the store is its own source of truth, so warm is
    the identity of get(); the round-2 VersionedFileStore (spec #323)
    and the catalog-warm pipeline plug in here without touching callers.

The content profiles (the per-item played state the rails read) and any
agent-installed profile share this one store — the LLM active profile
installs through the same ``install()`` seam as the production writes.

Round-2 persistence (spec #323, Store T1 #324): when ``Settings.profile_file``
is configured, the store persists through the shared ``VersionedFileStore``
— version token + atomic writes (ADR-0003's obligation for persisted
domain values) — restoring the profile on a cold start and saving on every
``install()``. Default unset: in-memory only, unchanged behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from . import config as _config
from .config import Settings
from .versioned_store import VersionedFileStore

#: Wire version of the persisted profile (bump on shape changes; the
#: store degrades to empty on unknown versions).
_PROFILE_VERSION = 1


def _encode_profile(profile: object) -> object:
    """Profile -> the JSON-serializable ``data`` value."""
    if not isinstance(profile, Profile):
        raise TypeError("profile payload expected")
    return {"played": dict(profile.played)}


def _decode_profile(data: object) -> Profile:
    """The ``data`` value -> Profile; raises on any shape mismatch."""
    if not isinstance(data, dict) or not isinstance(data.get("played"), dict):
        raise TypeError("profile envelope must be {'played': {item_id: position_ticks}}")
    played = {str(k): int(v) for k, v in data["played"].items()}
    return Profile(played=played)


@dataclass(frozen=True)
class Profile:
    """Typed, immutable viewer profile (the facade's single user, D4).

    ``played`` carries the resume memory: ``item_id -> position_ticks``
    for every item the client reported playing (ticket #214). A profile
    is a value — replacing it atomically via ``install()`` is how state
    changes, so readers never observe a half-written profile.
    """

    #: item_id -> position_ticks (most-progressed ordering is a read
    #: concern; the rails sort when they consume).
    played: Mapping[str, int] = field(default_factory=dict)


class ProfileStore:
    """The one seam for the active viewer profile (install/get/warm).

    Arch T12 (spec #309) configuration seam: the store is constructed
    with a settings argument (re-instantiable, the snapshot-store
    pattern) — a fresh store from new settings instead of import
    tricks. Round-2 (spec #323): ``settings.profile_file`` opts into
    versioned JSON persistence through the shared ``VersionedFileStore``;
    a cold store with a configured file restores the persisted profile
    (corrupt/mismatched files degrade to the empty default via the
    module's ladder) and every ``install()`` saves atomically. Install
    writes directly — resume reports in this tree are explicit, rare
    events; the module's ``DebouncedSave`` wrapper is there for adapters
    with high-frequency writes.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active: Profile = Profile()
        self._persist: VersionedFileStore | None = None
        if settings.profile_file is not None:
            self._persist = VersionedFileStore(
                path=settings.profile_file,
                supported_versions=(_PROFILE_VERSION,),
                encode=_encode_profile,
                decode=_decode_profile,
            )
            restored = self._persist.load()
            if isinstance(restored, Profile):
                self._active = restored

    def get(self) -> Profile:
        """The active profile. A cold store answers the empty default —
        callers never branch on whether the store has been warmed."""
        return self._active

    def install(self, profile: Profile) -> Profile:
        """Atomically replace the active profile — the single write seam.

        Every actor that changes the profile (playback reports, tests,
        agent/LLM setup, persistence restore) goes through this; nothing
        mutates the store's internals directly. When persistence is
        configured the new profile is saved atomically (never raises).
        Returns the installed profile so callers can chain.
        """
        self._active = profile
        if self._persist is not None:
            self._persist.save(profile)
        return profile

    def warm(self) -> Profile:
        """Materialize the active profile (cold → default/restored) and return it.

        warm is the identity of get(): a cold store with a configured
        file already restored the persisted profile at construction, so
        callers never branch on cold-vs-warm. It stays the hook where
        the catalog-warm pipeline and future reload strategies plug in.
        """
        return self._active


#: The module-level singleton every production caller uses. Tests reset
#: it through the seam (``install(Profile())``), never by mutating it.
profile_store = ProfileStore(_config.SETTINGS)
