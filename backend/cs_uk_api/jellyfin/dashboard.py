"""The facade's Dashboard surface conversation (spec #280).

ONE owner for the routes Switchfin's dashboard and download manager open
so they stop 404-ing: every one answers with honest data or an honest
empty, never a fake number. The surface is defined in ``CONTEXT.md``
§*Dashboard surface* (spec #280) — the spec set, not a line count:

  - ``/Items/Counts`` — movies/series from the home snapshot, episodes
    from the CACHED content pages only (``peek``, never a fetch).
  - ``/System/Info/Storage`` — real bytes for the one directory the
    facade writes (the poster cache) plus honest empty rows.
  - ``/Users`` — the single fixed facade user (D4).
  - ``/ScheduledTasks`` / ``/Devices`` / ``/System/ActivityLog/Entries``
    / ``/LiveTv/Programs/Recommended`` — the client's standard empty
    envelopes.
  - ``POST /Sessions/Capabilities/Full`` — every connect posts its
    capabilities; the facade answers 204 (nothing to store, D8).
  - ``POST /System/Restart`` — operator action, 204 first with the
    re-exec deferred one loop tick through the injectable seams.

**What is deliberately NOT here.** ``GET /Items/{id}/Download`` is defined
by this surface but lives in :mod:`cs_uk_api.jellyfin.delivery` — where the
bytes are — so a surface's routes and its module are not always the same
set (decided 2026-09-12, CONTEXT.md). The facade's remaining compat shims
(``/DisplayPreferences/usersettings``, ``/Items/{id}/SpecialFeatures``,
``/Sessions``, ``/ScheduledTasks/Running/llm-profile``,
``/Sessions/Logout``, ``/LiveTv/Channels``) belong to other conversations
and stay in :mod:`cs_uk_api.jellyfin.router` — the llm-profile trigger is
task control (spec #290), the rest are the browsing/remote-tab shims.

**Table position is load-bearing for ONE route.** FastAPI matches in
registration order, so ``/Items/Counts`` must be declared before
``/Items/{item_id}`` or ``Counts`` is swallowed as an item id and 404s.
That is why :func:`register` is invoked from ``router.py`` at the position
the route used to occupy rather than at the tail with the other moved
conversations.

Import direction: this module -> ``auth`` / ``identity`` / ``models`` /
``resolution`` (the shared facade pieces) and ``catalog`` only through
``resolution``. It receives the ``router`` at registration time.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ..config import SETTINGS
from .auth import require_token
from .identity import _server_id, _user_name_for
from .models import (
    ActivityLogEntryQueryResult,
    BaseItemDtoQueryResult,
    DeviceInfoDtoQueryResult,
    FolderStorageDto,
    ItemCounts,
    SystemStorageDto,
    UserDto,
)
from .resolution import _snapshot_counts


def _folder_storage(path: str) -> FolderStorageDto:
    """Storage row for one directory (spec #280): path, used bytes,
    free bytes from ``statvfs`` when the filesystem reports it.
    """
    used: int | None = None
    free: int | None = None
    try:
        if path and os.path.isdir(path):
            used = _dir_size(path)
            st = os.statvfs(path)
            free = st.f_bavail * st.f_frsize
    except OSError:  # a stat failure degrades to empty row
        pass
    return FolderStorageDto(Path=path or "", FreeSpace=free, UsedSpace=used)


def _dir_size(path: str) -> int:
    """Recursive byte total of ``path`` (cheap for one cache directory)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:  # a raced/unreadable file is skipped
                pass
    return total


def _storage_report() -> SystemStorageDto:
    """The dashboard storage table (spec #280).

    Real data only for the poster cache (``ImageCacheFolder`` — the one
    directory the facade actually writes) and the disk's free space;
    every other named folder is the honest empty row (no other on-disk
    state exists). ``Libraries`` is empty — the catalog is virtual.
    """
    poster_dir = SETTINGS.poster_cache_dir
    empty = FolderStorageDto(Path="")
    return SystemStorageDto(
        ProgramDataFolder=empty,
        WebFolder=empty,
        ImageCacheFolder=_folder_storage(poster_dir or ""),
        CacheFolder=empty,
        LogFolder=empty,
        InternalMetadataFolder=empty,
        TranscodingTempFolder=empty,
        Libraries=[],
    )


#: Injectable re-exec seam (spec #280): the real restart swaps the
#: running process for a fresh one via ``os.execv``; tests replace this
#: with a recorder so a ``/System/Restart`` test never spawns a real
#: restart. Kept as a module-level callable so the route body stays
#: declarative — and so tests can patch it HERE, its owner.
def _exec_restart() -> None:
    """Replace the running process with the same command line (uvicorn
    relaunch) — the dashboard restart button's real action."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


#: Injectable schedule seam (spec #280): the route answers 204 first
#: and lets ``_schedule_restart`` defer the re-exec to a later loop
#: tick; tests replace it with a recorder so no real restart (or loop
#: pump) is needed.
def _schedule_restart() -> None:
    """Defer the re-exec one tick after the 204 response is sent."""
    asyncio.get_event_loop().call_later(0.1, _exec_restart)


def register(router: APIRouter) -> None:
    """Attach the Dashboard surface to the facade router.

    Declaration order is preserved exactly as it was in ``router.py``:
    counts, storage, users, the graceful-empty envelopes, the capability
    POST, Live TV recommended, then the restart button. ``/Items/Counts``
    is FIRST here because it must precede the ``/Items/{item_id}`` detail
    route in the table (see the module docstring) — the caller's position
    for this call is what guarantees that, not this argument.
    """

    @router.get(
        "/Items/Counts",
        response_model=ItemCounts,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def items_counts() -> ItemCounts:
        """Dashboard library-size row (spec #280): snapshot-derived counts.

        Movies/series are the forms the merged home snapshot actually
        knows; episodes come from the cached content pages (never a fetch);
        everything else the Jellyfin counts envelope names is structurally
        zero for this catalog and stays omitted.
        """
        return _snapshot_counts()

    @router.get(
        "/System/Info/Storage",
        response_model=SystemStorageDto,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def system_storage() -> SystemStorageDto:
        """Dashboard storage table (spec #280): real bytes, honest empties.

        The poster-cache directory's footprint (``ImageCacheFolder``) and
        the disk's free space are recomputed per call — cheap for one
        directory. Every other folder row is empty: the facade keeps no
        other on-disk state.
        """
        return _storage_report()

    @router.get(
        "/Users",
        response_model=list[UserDto],
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def users_list() -> list[UserDto]:
        """Dashboard users row (spec #280): the single fixed facade user.

        The facade is single-user by design (D4); the list echoes that one
        user so the dashboard's users screen renders instead of erroring.
        """
        user_id = uuid.uuid5(uuid.NAMESPACE_URL, "cs-uk-api-user:default").hex
        return [UserDto(Name=_user_name_for(user_id), ServerId=_server_id(), Id=user_id)]

    @router.get(
        "/ScheduledTasks",
        response_model=list[object],
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def scheduled_tasks() -> list[object]:
        """Dashboard scheduled-tasks row (spec #280): an empty task list.

        There is no task scheduler (out of scope), so the list is honestly
        empty in the standard ``TaskInfo[]`` shape — never a 404. The
        on-demand LLM-profile trigger under this path is task CONTROL
        (spec #290) and lives in ``router.py``.
        """
        return []

    @router.get(
        "/Devices",
        response_model=DeviceInfoDtoQueryResult,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def devices() -> DeviceInfoDtoQueryResult:
        """Dashboard devices row (spec #280): an empty device list.

        The facade is stateless (D8) — no device sessions are tracked — so
        the list is honestly empty in the standard query-result shape.
        """
        return DeviceInfoDtoQueryResult(Items=[], TotalRecordCount=0)

    @router.get(
        "/System/ActivityLog/Entries",
        response_model=ActivityLogEntryQueryResult,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def activity_log_entries() -> ActivityLogEntryQueryResult:
        """Dashboard activity-log row (spec #280): an honest empty log.

        No activity is tracked, so the log answers the standard
        ``ActivityLogEntryQueryResult`` with zero entries — never a 404.
        """
        return ActivityLogEntryQueryResult(Items=[], TotalRecordCount=0, StartIndex=0)

    @router.post(
        "/Sessions/Capabilities/Full",
        dependencies=[Depends(require_token)],
    )
    async def sessions_capabilities_full() -> Response:
        """Client capability announcement (spec #280): accept, answer 204.

        Every Switchfin connect posts its playback capabilities here; a 404
        polluted startup logs. There is nothing to store — the facade is
        stateless (D8) — so the honest answer is 204.
        """
        return Response(status_code=204)

    @router.get(
        "/LiveTv/Programs/Recommended",
        response_model=BaseItemDtoQueryResult,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def live_tv_recommended() -> BaseItemDtoQueryResult:
        """Live TV recommended programs (spec #280): an empty listing.

        There is no live source (out of scope, same as ``/LiveTv/Channels``
        #257), so the recommended-programs query answers the standard empty
        ``BaseItemDtoQueryResult`` — never a 404.
        """
        return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)

    @router.post("/System/Restart", dependencies=[Depends(require_token)])
    async def system_restart() -> Response:
        """Dashboard restart button (spec #280): answer 204, then re-exec.

        The client expects a 204 response before the process disappears, so
        the re-exec is scheduled one event-loop tick AFTER the response is
        sent. ``_schedule_restart`` / ``_exec_restart`` are the injectable
        seams — LAN-only by design, an operator action (documented): the
        facade never exposes this over the open internet.
        """
        _schedule_restart()
        return Response(status_code=204)


__all__ = [
    "_dir_size",
    "_exec_restart",
    "_folder_storage",
    "_schedule_restart",
    "_storage_report",
    "register",
]
