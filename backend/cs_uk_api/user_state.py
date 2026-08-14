"""Persisted user-state store (ticket #258, spec #257).

The facade's favorites and played marks (tapped on Switchfin's detail
screen and card context menu) survive a backend restart: they live in a
single versioned JSON file next to the resume store's. Writes are
atomic (temp file + rename in the same directory) and synchronous —
every toggle lands on disk before the request returns, so the response
(UserDataResult) always reflects durable state.

A corrupt or version-mismatched file degrades to empty state — a
warning is logged and the API keeps serving; startup never crashes on
state. Kept deliberately SEPARATE from the resume store so the two
specs' version bumps never collide (spec #257 storage decision).

Single-process assumption (ADR-0003): one uvicorn worker owns the file;
writes are a full-snapshot temp+rename so a torn file is impossible
even across a power cut.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("cs_uk_api.user_state")

#: Format version of the user-state file (spec #257). Bump whenever the
#: on-disk shape changes; a file with a different version is ignored.
USER_STATE_VERSION = 1

#: Bounded list cap (spec #257): favorites/played are bounded with
#: dedupe. Far above any real library, but bounded so a pathological
#: client can't grow the file without limit.
MAX_MARKS = 256

#: Dub-memory cap (spec #276): at most 50 remembered series dubs,
#: least-recently-picked evicted (a repeat pick moves to the front).
MAX_DUB_MEMORY = 50


class UserStateStore:
    """Favorites + played marks as in-memory sets mirrored to a JSON
    file.

    ``set_favorite``/``set_played`` update the in-memory state and write
    the full snapshot synchronously (the request path — the client reads
    the flipped state back from the response, so the write must land
    before the request returns). ``flush()`` is a no-op aliased for the
    lifespan shutdown path symmetry; with ``path=None`` the store is
    memory-only — the test-suite default.
    """

    def __init__(self, path: str | None) -> None:
        self._path = path
        self._favorites: set[str] = set()
        self._played: set[str] = set()
        # Dub memory (spec #276): series group key -> translation label,
        # most-recently-picked first (newest wins on a repeat).
        self._dub_memory: dict[str, str] = {}
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        """Read the state file at construction (process start).

        Missing file → clean start. Unparseable JSON, a wrong version
        token, or a bad shape → warning + empty state; never a crash.
        """
        if self._path is None:
            return
        try:
            raw = Path(self._path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            log.warning("user state unreadable, starting empty: %s", self._path, exc_info=True)
            return
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("v") != USER_STATE_VERSION:
                log.warning(
                    "user state ignored: version mismatch or bad shape at %s (expected v%d)",
                    self._path,
                    USER_STATE_VERSION,
                )
                return
            self._favorites = self._clean_list(payload.get("favorites"))
            self._played = self._clean_list(payload.get("played"))
            # v1 dub-memory section (spec #276) — additive: older files
            # simply have none, never a crash on upgrade.
            dubs = payload.get("dub_memory")
            if isinstance(dubs, dict):
                self._dub_memory = {
                    str(k): str(v)
                    for k, v in dubs.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
        except Exception:  # a bad file must never crash startup
            log.warning("user state unreadable, starting empty: %s", self._path, exc_info=True)

    @staticmethod
    def _clean_list(raw: object) -> set[str]:
        if not isinstance(raw, list):
            return set()
        return {s for s in raw if isinstance(s, str)}

    def _write_now(self) -> None:
        """Full-snapshot atomic write (temp file + rename, same dir).

        Best-effort: a write failure is logged, never raised — state
        stays correct in memory and the next write retries.
        """
        if self._path is None:
            return
        try:
            path = Path(self._path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".user-state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "v": USER_STATE_VERSION,
                            "favorites": sorted(self._favorites),
                            "played": sorted(self._played),
                            "dub_memory": self._dub_memory,
                        },
                        fh,
                        ensure_ascii=False,
                    )
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:  # never take the API down over a write
            log.warning("user state write failed: %s", self._path, exc_info=True)

    # -- mutate / read ---------------------------------------------------

    def set_favorite(self, item_id: str, is_favorite: bool) -> None:
        """Mark or unmark an item as favorite (deduped, bounded)."""
        if is_favorite:
            self._favorites.add(item_id)
        else:
            self._favorites.discard(item_id)
        self._trim()
        self._write_now()

    def set_played(self, item_id: str, played: bool) -> None:
        """Mark or unmark an item as played (deduped, bounded)."""
        if played:
            self._played.add(item_id)
        else:
            self._played.discard(item_id)
        self._trim()
        self._write_now()

    def _trim(self) -> None:
        """Bound both lists at MAX_MARKS (spec #257: bounded with dedupe)."""
        if len(self._favorites) > MAX_MARKS:
            self._favorites = set(sorted(self._favorites)[-MAX_MARKS:])
        if len(self._played) > MAX_MARKS:
            self._played = set(sorted(self._played)[-MAX_MARKS:])

    def is_favorite(self, item_id: str) -> bool:
        return item_id in self._favorites

    def is_played(self, item_id: str) -> bool:
        return item_id in self._played

    def favorites(self) -> list[str]:
        return sorted(self._favorites)

    def played(self) -> list[str]:
        return sorted(self._played)

    def remember_dub(self, series_group_key: str, translation_label: str) -> None:
        """Record the viewer's dub choice for a series (spec #276):
        newest pick wins, bounded at ``MAX_DUB_MEMORY`` (least-recently-
        picked evicted). Synchronous write — the stream request that
        records the choice has already answered by the time the next
        PlaybackInfo for the series reads it.
        """
        self._dub_memory.pop(series_group_key, None)
        self._dub_memory[series_group_key] = translation_label
        while len(self._dub_memory) > MAX_DUB_MEMORY:
            oldest = next(iter(self._dub_memory))
            self._dub_memory.pop(oldest, None)
        self._write_now()

    def dub_for(self, series_group_key: str) -> str | None:
        """The remembered dub label for a series, or None."""
        return self._dub_memory.get(series_group_key)

    def dub_memory(self) -> dict[str, str]:
        """Full dub-memory map (spec #276), most-recently-picked first."""
        return dict(self._dub_memory)

    def flush(self) -> None:
        """Write any pending state now (lifespan shutdown symmetry).

        Writes are already synchronous per-toggle; this exists so the
        shutdown path can call the same hook for both stores.
        """
        self._write_now()

    def clear(self) -> None:
        """Drop all marks + dub memory (test isolation)."""
        self._favorites.clear()
        self._played.clear()
        self._dub_memory.clear()
