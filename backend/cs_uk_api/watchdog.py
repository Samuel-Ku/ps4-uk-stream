"""All-providers-down watchdog (ticket #215).

A long-running backend can silently lose ALL outbound connectivity
(network / VPN-state change underneath it) while its in-memory caches
keep serving 200s — every upstream times out at exactly the upstream
timeout, and detail/play degrade to 404 "item unavailable" after two
retry slots. The health tracker records the failures but nothing acts
on them, so a wedged process looks healthy until a user hits it.

This module detects that wedge — every non-marker provider reporting
``down`` simultaneously (never a legit steady state) — and resets the
shared httpx client (``http_client.close_client``), which is the
recovery a fresh process gets for free. A cooldown rate-limits resets
so a genuinely-down network can't churn a fresh client every tick.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Protocol

from .health import TRACKER
from .http_client import close_client
from .models import STATUS_DOWN, HealthStatus
from .providers import PROVIDERS

log = logging.getLogger("cs_uk_api")


class HealthSource(Protocol):
    """Minimal tracker surface the watchdog reads (HealthTracker-compatible)."""

    def status(self, provider_id: str) -> HealthStatus: ...

    def startup_marker(self, provider_id: str) -> str | None: ...

#: Default seconds between consecutive resets — a single reset is cheap,
#: but a genuinely-down network shouldn't make us spin a new client per
#: tick.
DEFAULT_COOLDOWN_S = 300.0


class WedgedClientWatchdog:
    """Detect the all-providers-down wedge and reset the shared client.

    Constructor arguments are injectable for tests: ``tracker`` and
    ``providers`` stand in for the module singletons, ``cooldown_s``
    shrinks the reset window.
    """

    def __init__(
        self,
        tracker: HealthSource = TRACKER,
        providers: Mapping[str, object] = PROVIDERS,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> None:
        self._tracker = tracker
        self._providers = providers
        self._cooldown_s = cooldown_s
        self._last_reset_at: float | None = None
        self.reset_count: int = 0

    def _startup_marker(self, provider_id: str) -> str | None:
        return self._tracker.startup_marker(provider_id)

    def all_relevant_down(self) -> bool:
        """True when EVERY provider not pinned by a startup marker is down.

        Providers pinned ``down`` at startup (e.g. uakino without its
        Chromium binary) are deterministic, not a wedge — they neither
        veto the signal nor satisfy it on their own. A provider with no
        samples yet reads ``ok`` (insufficient data), so a cold tracker
        at startup never fires.
        """
        relevant = [
            pid
            for pid in self._providers
            if self._startup_marker(pid) is None
        ]
        if not relevant:
            return False
        return all(self._tracker.status(pid) == STATUS_DOWN for pid in relevant)

    def should_reset(self) -> bool:
        """True when the wedge is detected and the cooldown has elapsed."""
        if not self.all_relevant_down():
            return False
        return not (
            self._last_reset_at is not None
            and time.monotonic() - self._last_reset_at < self._cooldown_s
        )

    async def check_and_reset(self) -> bool:
        """Run one watchdog tick: reset the client when warranted.

        Returns True when a reset actually happened. Safe to call
        repeatedly (cooldown-gated, idempotent).
        """
        if not self.should_reset():
            return False
        await close_client()
        self._last_reset_at = time.monotonic()
        self.reset_count += 1
        log.warning(
            "watchdog: all %d providers down simultaneously — reset shared "
            "httpx client (reset #%d)",
            len(self._providers),
            self.reset_count,
        )
        return True

    @property
    def last_reset_at(self) -> float | None:
        return self._last_reset_at

    @property
    def cooldown_s(self) -> float:
        return self._cooldown_s


#: Process-wide singleton the background task and /api/health read.
WATCHDOG = WedgedClientWatchdog()
