"""The process's background life: startup policy, loops, and teardown.

ONE owner for everything that lives across the app's lifetime — the
chromium startup marker, the four background loops (uakino
warm+heartbeat, watchdog, catalog warm, LLM taste profile), their task
handles, and ``lifespan`` with its bounded shutdown drain. main.py keeps
the app, the middlewares and the routes; this module is imported by
main and nothing else, so the teardown subtlety has a named, testable
home instead of living beside route handlers.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import catalog
from . import catalog_warm as catalog_warm_mod
from . import config as _config
from . import watchdog as watchdog_mod
from .health import TRACKER
from .http_client import close_client
from .llm import llm_enabled as _llm_enabled
from .torrent_engine import (
    ENGINE_PROBE_INTERVAL_S,
    ENGINE_TRACKER_ID,
    build_engine_from_settings,
    engine_configured,
    engine_half_configured,
    probe_engine,
)
from .uakino_browser import DEFAULT_CHROMIUM, get_session

log = logging.getLogger("cs_uk_api")

# uakino's browser-session provider cannot work without a system Chromium
# binary (v3 spec §2.1): mark it down deterministically at startup instead
# of letting it fail per-request.
if not os.path.exists(DEFAULT_CHROMIUM):
    TRACKER.mark_startup("uakino", "chromium_missing")
    log.warning("uakino marked down at startup: chromium binary not found at %s", DEFAULT_CHROMIUM)

#: Bounded drain for the background warm/heartbeat task in lifespan
#: shutdown so a mid-warm Chromium launch cannot hang the teardown.
_WARM_TASK_DRAIN_S: float = 1.0

#: Handle of the background warm+heartbeat task started by ``lifespan``.
_warm_task: asyncio.Task[None] | None = None

#: Latest observable state of the startup catalog warm (#204/#210).
#: None until the task has run at least once; updated in place by
#: ``_catalog_warm_loop`` so ``/api/health`` can surface it.
_catalog_warm_state: catalog_warm_mod.CatalogWarmState | None = None

#: Handle of the background catalog-warm task started by ``lifespan``.
_catalog_warm_task: asyncio.Task[None] | None = None

#: Handle of the background watchdog task started by ``lifespan``
#: (ticket #215).
_watchdog_task: asyncio.Task[None] | None = None

#: Watchdog tick period: how often the all-providers-down check runs.
_WATCHDOG_INTERVAL_S: float = 60.0

#: Handle of the background LLM taste-profile refresh task started by
#: ``lifespan`` (spec #290) — None until scheduled.
_llm_task: asyncio.Task[None] | None = None

#: Daily LLM taste-profile cadence (spec #290 §Cadence: "refreshed
#: daily in the background").
_LLM_PROFILE_INTERVAL_S: float = 24 * 60 * 60

#: Handle of the background engine-probe task started by ``lifespan``
#: (spec #394) — None when the engine is unconfigured or the knob is 0.
_engine_probe_task: asyncio.Task[None] | None = None


def mark_engine_startup_state() -> None:
    """Deterministic engine-entry marker (spec #394).

    A half-configured engine (auth pair split) or a malformed base URL
    is a fault that will never heal on its own — pin ``yts:engine``
    down at startup (the uakino chromium_missing convention) instead of
    letting the probe window or a stream-time 401 tell the operator.
    Unconfigured = invisible (a deployment choice, not a fault);
    fully configured = nothing to mark.
    """
    s = _config.SETTINGS
    if not (s.torrent_engine_url or "").strip():
        return
    if engine_half_configured(s) or not engine_configured(s):
        TRACKER.mark_startup(ENGINE_TRACKER_ID, "engine_misconfigured")
        log.warning(
            "yts:engine marked down at startup: engine misconfigured "
            "(split auth pair or schemeless URL)"
        )


async def _engine_probe_loop() -> None:
    """Engine liveness probe (spec #394).

    Scheduled once by ``lifespan`` when the engine is configured and
    the interval knob is non-zero. Each tick probes the engine's
    capabilities endpoint (ANY HTTP answer = alive) and records the
    boolean into the ``yts:engine`` sliding window, so a dead engine
    is visible on /api/providers between play presses. A tick failure
    must never kill the loop — log and move on.
    """
    while True:
        await asyncio.sleep(_config.SETTINGS.engine_probe_interval_s or ENGINE_PROBE_INTERVAL_S)
        try:
            engine = build_engine_from_settings()
            if engine is None:
                continue  # unconfigured mid-flight: nothing to probe
            base = getattr(engine, "_base_url", None)
            if base:
                await probe_engine(base, record=True)
        except Exception:
            log.exception("engine probe tick failed")


def catalog_warm_state() -> catalog_warm_mod.CatalogWarmState | None:
    """The warm loop's latest observable state (``None`` before first run).

    The accessor ``/api/health`` reads instead of reaching into this
    module's global — the loop owns the state, the route only asks.
    """
    return _catalog_warm_state


async def _llm_profile_loop() -> None:
    """Daily LLM taste-profile refresh (spec #290 user story 10).

    Scheduled once by ``lifespan`` — and ONLY when the three LLM knobs
    are configured (the layer is invisible until enabled). Each tick
    refreshes the active profile from the current signals; the refresh
    function never raises, so a failed model call can never kill the
    loop. Cancelled by ``lifespan`` shutdown.
    """
    while True:
        await asyncio.sleep(_LLM_PROFILE_INTERVAL_S)
        await catalog.refresh_profile()


async def _watchdog_loop() -> None:
    """Periodic all-providers-down check (ticket #215).

    Scheduled once by ``lifespan``. Each tick asks the shared watchdog
    whether EVERY non-marker provider is down simultaneously and, if so,
    resets the shared httpx client (cooldown-gated). A tick failure must
    never kill the loop — log and move on.
    """
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL_S)
        try:
            await watchdog_mod.WATCHDOG.check_and_reset()
        except Exception:
            log.exception("watchdog tick failed")


async def _catalog_warm_loop() -> None:
    """Background catalog warm (tickets #204/#210).

    Scheduled once by ``lifespan``. Builds the home snapshot, then
    warms each view's first-card detail chain — so a real client's
    first ``/UserViews`` / ``/Items`` / card-open after launch finds
    warm caches instead of a 17-21s cold scrape that blows the app's
    own request timeout. Best-effort: a provider failure never crashes
    the process; the outcome is observable via ``/api/health``.
    """
    global _catalog_warm_state
    _catalog_warm_state = await catalog_warm_mod.warm_catalog()
    log.info(
        "catalog warm done: home_warmed=%s content_warmed=%d failed=%d",
        _catalog_warm_state.home_warmed,
        _catalog_warm_state.content_warmed,
        _catalog_warm_state.failed,
    )


async def _warm_and_heartbeat() -> None:
    """Background uakino warm + heartbeat (issue #193/#195).

    Scheduled once by ``lifespan``. ``warm()`` failures are pinned as
    deterministic startup markers so explicit uakino routes short-circuit
    502 instead of blocking on a session that can never serve; success
    hands off to the heartbeat loop, which records ok/fail per tick into
    TRACKER — the sliding-window state ``/api/providers`` and the fan-out
    skip read. Cancelled by ``lifespan`` shutdown.
    """
    session = get_session()
    try:
        await session.warm()
    except TimeoutError:
        TRACKER.mark_startup("uakino", "warm_timeout")
        log.warning("uakino warm timed out; marked down at startup")
        return
    except Exception as e:  # noqa: BLE001
        TRACKER.mark_startup("uakino", "warm_failed")
        log.warning("uakino warm failed; marked down at startup: %s", e)
        return
    await session.heartbeat_loop(record=lambda ok: TRACKER.record("uakino", ok))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _warm_task, _watchdog_task, _catalog_warm_task, _llm_task, _engine_probe_task
    mark_engine_startup_state()
    if os.path.exists(DEFAULT_CHROMIUM):
        # Background warm+heartbeat (issue #193): uakino's browser session
        # is brought up once at startup instead of lazily on first request,
        # so its health is known before a client asks for it.
        _warm_task = asyncio.create_task(_warm_and_heartbeat())
    # Background watchdog (ticket #215): a long-running process can lose
    # ALL outbound connectivity while caches still serve 200s; this loop
    # detects every-provider-down and resets the shared httpx client.
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    # Background catalog warm (#204/#210): build the home snapshot and
    # warm the first-card detail chain before a client drives, so the
    # app's first requests never hit a 17-21s cold scrape. OFF in tests
    # (conftest sets CS_UK_CATALOG_WARM=0) so a TestClient lifespan
    # never triggers real provider scrapes.
    if _config.SETTINGS.catalog_warm_enabled:
        _catalog_warm_task = asyncio.create_task(_catalog_warm_loop())
    # Daily LLM taste-profile refresh (spec #290): scheduled only when
    # the layer is configured — no knobs, no task, no LLM calls.
    if _llm_enabled():
        _llm_task = asyncio.create_task(_llm_profile_loop())
    # Engine liveness probe (spec #394): only when the engine is
    # configured and the cadence knob is non-zero — unconfigured means
    # invisible, 0 means the operator turned the loop off.
    if engine_configured(_config.SETTINGS) and _config.SETTINGS.engine_probe_interval_s > 0:
        _engine_probe_task = asyncio.create_task(_engine_probe_loop())
    yield
    if _watchdog_task is not None:
        _watchdog_task.cancel()
        try:
            await asyncio.wait_for(_watchdog_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        _watchdog_task = None
    if _catalog_warm_task is not None:
        _catalog_warm_task.cancel()
        try:
            await asyncio.wait_for(_catalog_warm_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        _catalog_warm_task = None
    if _warm_task is not None:
        _warm_task.cancel()
        try:
            await asyncio.wait_for(_warm_task, timeout=_WARM_TASK_DRAIN_S)
        except (TimeoutError, asyncio.CancelledError):
            pass
        _warm_task = None
    if _llm_task is not None:
        _llm_task.cancel()
        try:
            await asyncio.wait_for(_llm_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        _llm_task = None
    if _engine_probe_task is not None:
        _engine_probe_task.cancel()
        try:
            await asyncio.wait_for(_engine_probe_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        _engine_probe_task = None
    # Persist any debounced playback-progress state (ticket #248): the
    # Stopped report already flushed, but heartbeat positions may still
    # be pending a debounce when the process is told to stop.
    catalog.flush_playback()
    # The uakino browser session is lazily created on first request and
    # runs a headless Chromium; close it on shutdown so SIGTERM doesn't
    # orphan the browser process. `close()` is a no-op when the session
    # was never started.
    await get_session().close()
    await close_client()
