"""Playback-report ingestion (the #108 Sessions conversation).

ONE owner for the client's playback-report surface: the three
``/Sessions`` POST routes the @jellyfin/sdk reports through
(PlaybackStartInfo / PlaybackProgressInfo / PlaybackStopInfo) and the
report-shape parser that turns a body into a resume-store call.

Extracted verbatim from :mod:`router` (safe refactor) — the parser sat
~1,300 lines from its only callers. The routes are declared on the
facade's own router by :func:`register` (kept FLAT — see that
docstring), so the wire surface (paths, status codes, the
``require_token`` gate, 204-always posture) is unchanged.

Parsing rule (#214/#248): ``ItemId`` + ``PositionTicks`` are what a
resume shelf needs; ``RunTimeTicks`` rides along for later
finished-item marking (spec #247). A malformed body is NOT an error —
the report is advisory; every report answers 204.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response

from ..catalog import record_position
from .auth import require_token

log = logging.getLogger(__name__)


async def record_playback_from(request: Request, *, flush: bool) -> None:
    """Best-effort store of the client's playback report (#214/#248).

    The @jellyfin/sdk posts PlaybackStartInfo/ProgressInfo/StopInfo
    bodies here; ``ItemId`` + ``PositionTicks`` are what a resume shelf
    needs, and ``RunTimeTicks`` rides along so a later tranche can mark
    finished items (spec #247). A malformed body is not an error — the
    report is advisory. ``flush=True`` (the Stopped path) persists the
    state file synchronously; heartbeat reports are debounced.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed report, keep the 204
        log.debug("playback report body unreadable, ignoring")
        return
    item_id = body.get("ItemId")
    position = body.get("PositionTicks")
    if isinstance(item_id, str) and isinstance(position, (int, float)):
        runtime = body.get("RunTimeTicks")
        runtime_ticks = int(runtime) if isinstance(runtime, (int, float)) else None
        record_position(item_id, int(position), runtime_ticks=runtime_ticks, flush=flush)


async def sessions_playing(request: Request) -> Response:
    """Playback-start report (D8): accept, answer 204, record position.

    The @jellyfin/sdk posts a full PlaybackStartInfo body here the moment
    playback starts (capture row 6); the position seeds the resume shelf
    (ticket #214; persisted per #248).
    """
    await record_playback_from(request, flush=False)
    return Response(status_code=204)


async def sessions_progress(request: Request) -> Response:
    """Playback-progress report (D8): accept, answer 204, record position.

    Heartbeats update the stored position (debounced write, #248); the
    newest report wins.
    """
    await record_playback_from(request, flush=False)
    return Response(status_code=204)


async def sessions_stopped(request: Request) -> Response:
    """Playback-stop report (D8): accept, answer 204, record the stop
    position — the final value the resume shelf shows (ticket #214).
    Flushed to the state file immediately (#248), so the position
    survives a restart.
    """
    await record_playback_from(request, flush=True)
    return Response(status_code=204)


def register(parent: APIRouter) -> None:
    """Declare the report routes on the facade router.

    Kept FLAT on the facade's own router — deliberately NOT a nested
    ``include_router``: this FastAPI line wraps nested includes in a
    lazy router object without ``path_format``, which the app's
    case-normalize middleware (``main.jellyfin_case_normalize``, via
    ``normalize_jellyfin_path``) reads on every facade request. The
    ``add_api_route`` calls below are the same registration the
    ``@router.post`` decorators did in :mod:`router` — same paths, same
    order, same dependency gate, same endpoints.
    """
    parent.add_api_route(
        "/Sessions/Playing", sessions_playing, methods=["POST"],
        dependencies=[Depends(require_token)],
    )
    parent.add_api_route(
        "/Sessions/Playing/Progress", sessions_progress, methods=["POST"],
        dependencies=[Depends(require_token)],
    )
    parent.add_api_route(
        "/Sessions/Progress", sessions_progress, methods=["POST"],
        dependencies=[Depends(require_token)],
    )
    parent.add_api_route(
        "/Sessions/Playing/Stopped", sessions_stopped, methods=["POST"],
        dependencies=[Depends(require_token)],
    )
    parent.add_api_route(
        "/Sessions/Stopped", sessions_stopped, methods=["POST"],
        dependencies=[Depends(require_token)],
    )
