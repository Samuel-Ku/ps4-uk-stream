"""In-memory provider health tracking (issue #53, v3 spec §2.1.3/§3.4).

Sliding window of recent upstream-operation outcomes per provider:

    samples < min_samples                      -> "ok"  (insufficient data)
    error_rate >= down_at                      -> "down"
    error_rate >= degraded_at                  -> "degraded"
    otherwise                                  -> "ok"

Deterministic startup markers (e.g. uakino missing its Chromium binary)
force "down" regardless of samples. No persistence: a backend restart is a
clean slate and a recovered site self-heals through the window.
"""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from .models import STATUS_DEGRADED, STATUS_DOWN, STATUS_OK, HealthStatus


class HealthTracker:
    def __init__(
        self,
        window: int = 20,
        min_samples: int = 5,
        degraded_at: float = 0.4,
        down_at: float = 0.8,
    ) -> None:
        self._window = window
        self._min_samples = min_samples
        self._degraded_at = degraded_at
        self._down_at = down_at
        self._samples: dict[str, deque[bool]] = {}
        self._errors: dict[str, datetime] = {}
        self._markers: dict[str, str] = {}

    def record(self, provider_id: str, ok: bool) -> None:
        samples = self._samples.setdefault(provider_id, deque(maxlen=self._window))
        samples.append(ok)
        if not ok:
            self._errors[provider_id] = datetime.now(UTC)

    def mark_startup(self, provider_id: str, reason: str) -> None:
        """Deterministically pin a provider as down (logged once at startup)."""
        self._markers[provider_id] = reason

    def startup_marker(self, provider_id: str) -> str | None:
        """Return the startup-marker reason for a provider, or None.

        Lets the API layer distinguish a deterministic startup failure
        (e.g. uakino's ``chromium_missing`` / ``warm_failed`` /
        ``warm_timeout``) from a transient sliding-window ``down``.
        """
        return self._markers.get(provider_id)

    def status(self, provider_id: str) -> HealthStatus:
        if provider_id in self._markers:
            return STATUS_DOWN
        samples = self._samples.get(provider_id)
        if not samples or len(samples) < self._min_samples:
            return STATUS_OK
        error_rate = 1.0 - (sum(samples) / len(samples))
        if error_rate >= self._down_at:
            return STATUS_DOWN
        if error_rate >= self._degraded_at:
            return STATUS_DEGRADED
        return STATUS_OK

    def last_error_at(self, provider_id: str) -> str | None:
        dt = self._errors.get(provider_id)
        return dt.isoformat(timespec="seconds") if dt is not None else None

    def reset(self) -> None:
        self._samples.clear()
        self._errors.clear()
        self._markers.clear()


#: ProviderError codes that are deterministic item/client-level verdicts,
#: NOT lane health (ADR-0002: 404 codes are client-side semantics; the
#: native route's ``record_skip_codes`` and the facade's stream path
#: encode the same taxonomy). These never flip the tracker — five
#: dead-title clicks must not starve a whole lane's rows (the #373
#: finding).
ITEM_VERDICT_CODES = frozenset(
    {"gated", "not_found", "invalid_translation", "translation_missing"}
)

#: The complementary set: codes that ARE lane health (transport, upstream
#: 5xx, data-shape breakage, and the search fan-out's synthetic rows).
#: ``is_classified`` + the drift test in tests/test_verdict_drift.py keep
#: this complete: a new ProviderError code must land in one of the two
#: sets or CI fails.
LANE_VERDICT_CODES = frozenset(
    {"unreachable", "upstream_unreachable", "parse_failed", "timeout", "internal"}
)


def is_classified(code: str) -> bool:
    """True when a ProviderError/ProviderFailure code has a verdict class."""
    return code in ITEM_VERDICT_CODES or code in LANE_VERDICT_CODES


def record_verdict(provider_id: str, code: str | None) -> None:
    """Record a provider outcome by its verdict code (single entry point).

    Item-level codes (``ITEM_VERDICT_CODES``) are client-side semantics,
    not upstream health — they are NOT recorded, matching the facade's
    stream path and the native route's ``record_skip_codes``. ``None``
    stands for a generic failure (a non-ProviderError exception) and is
    recorded as a lane fault, as is any lane/unknown code.
    """
    if code in ITEM_VERDICT_CODES:
        return
    TRACKER.record(provider_id, ok=False)


TRACKER = HealthTracker()
