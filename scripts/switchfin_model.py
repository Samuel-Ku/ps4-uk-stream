"""Shared data model + constants for the Switchfin manual-test runner.

Kept free of I/O so both the ADB layer and the report layer can import it
without a cycle. The runner core (``scripts/switchfin_test.py``) imports
from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: The request-log middleware line: ``METHOD path -> status (ms)``.
#: ``request.url.path`` carries no query string, so a library listing
#: ``GET /Users/{user}/Items?parentId=…`` logs as ``GET /Users/{user}/Items``.
LOG_LINE_RE = re.compile(
    r"(?P<method>\S+) (?P<path>[^ ]+) -> (?P<status>\d+) \(\d+ms\)"
)

#: Error markers that flip a step to ❌ when they appear in its logcat
#: window (ticket #146). Matched case-insensitively as substrings.
LOGCAT_ERROR_PATTERNS = (
    "type_error",
    "json::exception",
    "nlohmann",
    "DECODE",
    "http status 4",
    "http status 5",
    "PLAYER",
    "media_too_short",
    "OpenCodecContext",
    "avformat",
)

#: Home-row label per view routing key (mirrors ``home.py`` row titles).
VIEW_LABELS = {
    "newest": "Новинки",
    "popular": "Популярні зараз",
    "movie": "Фільми",
    "series": "Серіали",
    "anime": "Аніме",
    "cartoon": "Мультфільми",
    "dorama": "Дорами",
}

#: UI elements the ``--calibrate`` walkthrough records, in tap order.
CALIBRATION_ELEMENTS = (
    "login_button",
    "sidebar_folders",
    "view_newest_x",
    "view_popular_x",
    "view_movie_x",
    "view_series_x",
    "view_anime_x",
    "view_cartoon_x",
    "view_dorama_x",
    "first_card",
    "play_button",
    "first_season",
    "first_episode",
)


@dataclass(frozen=True)
class Expect:
    """One expected request: a regex over the full log line + allowed status."""

    request: str
    status: tuple[int, ...]
    capture: str | None = None  # context key to store a named regex group from


@dataclass(frozen=True)
class PlayTap:
    """One tap of a play sequence, with the requests it must trigger."""

    tap: str
    expects: tuple[Expect, ...]


@dataclass(frozen=True)
class Branch:
    """Type-conditional play branch (ticket #146): Movie vs Series."""

    key: str
    taps: tuple[PlayTap, ...]


@dataclass(frozen=True)
class Step:
    name: str
    phase: str  # handshake | warmup | open | detail | play | nav
    view: str | None
    tap: str | None  # calibration key for open/detail steps
    expects: tuple[Expect, ...]
    branches: tuple[Branch, ...] = ()
    # self-issued handshake request (ticket #144, no phone needed)
    method: str | None = None
    path: str | None = None
    body: dict[str, Any] | None = None
    capture_token: bool = False
    use_token: bool = False
    #: Data-only view uuid (issue #151). For ``phase: warmup`` steps the
    #: runner builds ``GET /Users/{user}/Items?parentId={view_id}`` from it
    #: (device-driving B1): the cold scrape takes ~21s, which blows both the
    #: app's own request timeout dialog and the step-detection window, so
    #: the runner primes each view's cache before the phone taps it.
    view_id: str | None = None
    #: Number of BACK presses for a ``phase: nav`` step (device-driving B6):
    #: the real client needs BACK between per-view steps (player -> detail ->
    #: library -> grid). Calibrated per device (4 on the OnePlus 8 Pro).
    nav: int = 0


@dataclass(frozen=True)
class StepResult:
    name: str
    phase: str
    view: str | None
    ok: bool
    skipped: bool = False
    timed_out: bool = False
    note: str = ""
    window_lines: tuple[str, ...] = ()
    logcat_hits: tuple[str, ...] = ()
    logcat_window: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportMeta:
    date: str
    android: str
    build: str
    backend_url: str
    phone: str
    resolution: str
    #: True when an adb device was attached for the run. A headless run is
    #: never a real-client verification; the report must say so explicitly
    #: rather than look like a checked-off device pass (issue #152).
    verified: bool = False


@dataclass(frozen=True)
class CaptureWindow:
    """Inclusive [start_ts, end_ts] host-epoch window for capture slicing.

    The runner records ``time.time()`` at the START/END marker emission
    points (ticket #147); the capture middleware stamps each record with the
    same host's ``time.time()``, so one clock orders both sides.
    """

    start_ts: float
    end_ts: float

    def contains(self, ts: object) -> bool:
        """True when a record's ``ts``, if numeric, falls inside the window."""
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return False
        return self.start_ts <= ts <= self.end_ts


def status_of(line: str) -> int | None:
    m = LOG_LINE_RE.search(line)
    return int(m.group("status")) if m else None
