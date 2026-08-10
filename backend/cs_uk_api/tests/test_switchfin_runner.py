"""Unit tests for the Switchfin manual-test runner (tickets #144-#146).

Runs inside the backend pytest suite, but exercises ONLY the runner's
parsing/sequencing logic — ADB subprocess calls and the backend request
log are replaced by scripted fakes, so nothing touches a device or an
HTTP server. The backend conftest only puts ``backend/`` on ``sys.path``;
this file adds the repo root so ``scripts.switchfin_test`` imports.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Self, cast
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # type: ignore[import-untyped]
from scripts.switchfin_adb import (  # type: ignore[import-not-found]
    Adb,
    LogTailer,
    read_getevent,
)
from scripts.switchfin_model import (  # type: ignore[import-not-found]
    CALIBRATION_ELEMENTS,
    CaptureWindow,
    ReportMeta,
    Step,
    StepResult,
)
from scripts.switchfin_report import (  # type: ignore[import-not-found]
    apply_logcat_filter,
    render_report,
    run_exit_code,
    write_snapshots,
)
from scripts.switchfin_test import (  # type: ignore[import-not-found]
    Runner,
    capture_in_window,
    find_play_pill,
    issue_path,
    load_steps,
    load_tap_coords,
    reset_capture_dir,
    splice_capture,
    splice_restart_step,
)

# --------------------------------------------------------------------------
# fakes: a scripted backend.log and a scripted adb device
# --------------------------------------------------------------------------


class FakeTailer:
    """Shares a list with FakeAdb so taps/issues append the lines they trigger."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def all_lines(self) -> list[str]:
        return self._lines


class FakeAdb:
    """Records taps/markers; appends scripted log lines on each tap."""

    def __init__(
        self,
        lines: list[str],
        tap_lines: dict[tuple[int, int], list[str]],
    ) -> None:
        self._lines = lines
        self._tap_lines = tap_lines
        self.taps: list[tuple[int, int]] = []
        self.backs: int = 0
        self.markers: list[str] = []
        self.restarts: int = 0
        self._available = True

    def available(self) -> bool:
        return self._available

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        self._lines.extend(self._tap_lines.get((x, y), []))

    def back(self) -> None:
        self.backs += 1

    def restart_app(self) -> None:
        self.restarts += 1
        # the real app reconnects by polling /System/Info on relaunch
        self._lines.append("GET /System/Info -> 200 (0ms)")

    def marker(self, text: str) -> None:
        self.markers.append(text)

    def logcat_dump(self) -> list[str]:
        return []

    def shell(self, command: str) -> str:
        return "n/a"

    def screenshot_png(self) -> bytes:
        # Not a real frame: find_play_pill(b"") raises -> the default locator
        # returns None -> the retry loop falls back to the calibrated tap.
        return b""


# --------------------------------------------------------------------------
# fixture data (mirrors the shipped steps.yaml shape, two views)
# --------------------------------------------------------------------------

# Single source for the gk capture regex (#154): the fixture template
# embeds the __GK_REQUEST__ marker, expanded from GK_REQUEST at import time,
# so a one-character tightening lands at exactly one site instead of two.
GK_REQUEST = "GET (/Users/[^ ]+)?/Items/(?P<gk>[^ /]+) -> 200"

_STEPS_YAML_TEMPLATE = """\
timeout_s: 0.4
steps:
  - name: login
    phase: handshake
    method: POST
    path: /Users/AuthenticateByName
    body: {Username: switchfin-test, Pw: switchfin-test}
    capture_token: true
    expect:
      - request: "POST /Users/AuthenticateByName -> 200"
        status: 200
  - name: views
    phase: handshake
    method: GET
    path: /UserViews
    use_token: true
    expect:
      - request: "GET /UserViews -> 200"
        status: 200
  - name: warmup_newest
    phase: warmup
    view_id: v_newest
    use_token: true
    expect:
      - request: "GET (/Users/[^ ]+)?/Items -> 200"
        status: 200
  - name: open_view_newest
    phase: open
    view: newest
    tap: view_newest_x
    expect:
      - request: "GET (/Users/[^ ]+)?/Items -> 200"
        status: 200
  - name: open_first_card_newest
    phase: detail
    view: newest
    tap: first_card
    expect:
      - request: __GK_REQUEST__
        status: 200
        capture: gk
  - name: play_newest
    phase: play
    view: newest
    branches:
      movie:
        - tap: play_button
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|206|302)"
              status: [200, 206, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
      series:
        - tap: first_season
          expect:
            - request: "GET /Shows/[^ ]+/Episodes -> 200"
              status: 200
        - tap: first_episode
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|206|302)"
              status: [200, 206, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
  - name: back_to_grid
    phase: nav
    nav: 4
  - name: open_view_movie
    phase: open
    view: movie
    tap: view_movie_x
    expect:
      - request: "GET (/Users/[^ ]+)?/Items -> 200"
        status: 200
  - name: open_first_card_movie
    phase: detail
    view: movie
    tap: first_card
    expect:
      - request: __GK_REQUEST__
        status: 200
        capture: gk
  - name: play_movie
    phase: play
    view: movie
    branches:
      movie:
        - tap: play_button
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|206|302)"
              status: [200, 206, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
      series:
        - tap: first_season
          expect:
            - request: "GET /Shows/[^ ]+/Episodes -> 200"
              status: 200
        - tap: first_episode
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|206|302)"
              status: [200, 206, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
"""

STEPS_YAML = _STEPS_YAML_TEMPLATE.replace("__GK_REQUEST__", GK_REQUEST)

TAPS: dict[str, tuple[int, int]] = {
    "sidebar_folders": (80, 100),
    "view_newest_x": (100, 200),
    "view_movie_x": (400, 200),
    "first_card": (500, 300),
    "play_button": (500, 400),
    "first_season": (500, 500),
    "first_episode": (500, 550),
}

FULL_TAP_LINES: dict[tuple[int, int], list[str]] = {
    # device-driving B21: the sidebar folders icon opens the Views grid
    (80, 100): ["GET /UserViews -> 200 (0ms)"],
    (100, 200): ["GET /Users/u1/Items -> 200 (0ms)"],
    (400, 200): ["GET /Users/u1/Items -> 200 (0ms)"],
    (500, 300): [
        "GET /Users/u1/Items/g1 -> 200 (0ms)",
        "GET /Items/g1/Images/Primary -> 200 (0ms)",
    ],
    (500, 400): [
        "POST /Items/g1/PlaybackInfo -> 200 (0ms)",
        "GET /Videos/g1/stream -> 302 (0ms)",
        "POST /Sessions/Playing -> 204 (0ms)",
    ],
    (500, 500): ["GET /Shows/g1/Episodes -> 200 (0ms)"],
    # device-driving B7: the episode-row tap auto-plays on the real client,
    # so first_episode fires the whole playback sequence by itself.
    (500, 550): [
        "POST /Items/e1/PlaybackInfo -> 200 (0ms)",
        "GET /Videos/e1/stream -> 302 (0ms)",
        "POST /Sessions/Playing -> 204 (0ms)",
    ],
}


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def make_harness(
    tmp_path: Path,
    *,
    steps_yaml: str = STEPS_YAML,
    taps: dict[str, tuple[int, int]] = TAPS,
    tap_lines: dict[tuple[int, int], list[str]] | None = None,
    probe: str = "Movie",
    issue_ok: bool = True,
    adb: FakeAdb | None = None,
    probe_fn: Callable[[dict[str, str]], str | None] | None = None,
    play_locator: Callable[[], tuple[int, int] | None] | None = None,
    #: fast failure-path tests: a 1s override keeps max(timeout_s, 1.0)
    #: at the fixture's 8s step timeout instead of PLAY_TIMEOUT_S (B22)
    play_timeout_s: float = 1.0,
) -> tuple[Runner, FakeTailer, FakeAdb]:
    """Build a Runner whose taps/issues append scripted backend.log lines."""
    steps_path = tmp_path / "steps.yaml"
    steps_path.write_text(steps_yaml, encoding="utf-8")
    _write_yaml(
        tmp_path / "tap-coords.yaml",
        {key: {"x": x, "y": y} for key, (x, y) in taps.items()},
    )
    timeout_s, steps = load_steps(steps_path)
    tap_coords = load_tap_coords(tmp_path / "tap-coords.yaml")
    if adb is None:
        lines: list[str] = []
        adb = FakeAdb(
            lines=lines, tap_lines=FULL_TAP_LINES if tap_lines is None else tap_lines
        )
    else:
        # the injected adb's own line store is the one the tailer watches, so
        # a successful tap's request lines are visible to step detection
        lines = adb._lines
    tailer = FakeTailer(lines)

    def issue(step: Step, ctx: dict[str, str]) -> None:
        if not issue_ok:
            return
        # warmup steps omit method/path — the real issuer defaults to GET and
        # builds the path from view_id; mirror both here
        method = step.method or "GET"
        path = issue_path(step, ctx)
        lines.append(f"{method} {path.split('?')[0]} -> 200 (0ms)")
        if step.capture_token:
            ctx["token"] = "tok"
            ctx["user_id"] = "u1"

    runner = Runner(
        steps,
        tap_coords,
        cast(LogTailer, tailer),
        cast(Adb, adb),
        host="0.0.0.0",
        port=8000,
        timeout_s=timeout_s,
        issue_fn=issue,
        probe_fn=probe_fn or (lambda _ctx: probe),
        play_locator_fn=play_locator,
        play_timeout_s=play_timeout_s,
    )
    return runner, tailer, adb


def _by_name(results: list[object]) -> dict[str, object]:
    return {r.name: r for r in results}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# (a) every step gets a verdict in the report
# --------------------------------------------------------------------------


def test_every_step_gets_verdict_in_report(tmp_path: Path) -> None:
    runner, _, _ = make_harness(tmp_path)
    results = runner.run()

    assert len(results) == 10  # 2 handshake + 1 warmup + 2 views × (open+detail+play) + nav
    assert all(r.ok for r in results), [r.note for r in results if not r.ok]

    meta = ReportMeta(
        date="2026-08-08",
        android="13",
        build="n/a",
        backend_url="http://0.0.0.0:8000",
        phone="Pixel",
        resolution="1080x2400",
    )
    report = render_report(results, meta)
    # handshake verdicts carry step names…
    for result in results:
        if result.phase == "handshake":
            assert result.name in report
    # …and every open/detail/play step lands in its view's table row
    assert "View sweep" in report
    assert "| Новинки | ✅ | ✅ | ✅ |" in report
    assert "| Фільми | ✅ | ✅ | ✅ |" in report
    assert "PASS ✅" in report
    assert run_exit_code(results) == 0


# --------------------------------------------------------------------------
# #152: the report states explicitly whether the run was verified on a
# device — a headless run must not read like a checked-off device pass
# --------------------------------------------------------------------------


def test_report_marks_run_verified_on_device(tmp_path: Path) -> None:
    runner, _, _ = make_harness(tmp_path)
    results = runner.run()
    meta = ReportMeta(
        date="2026-08-08",
        android="13",
        build="n/a",
        backend_url="http://0.0.0.0:8000",
        phone="Pixel",
        resolution="1080x2400",
        verified=True,
    )

    report = render_report(results, meta)

    assert "✅ verified on device" in report
    assert "unverified" not in report


def test_report_marks_headless_run_unverified(tmp_path: Path) -> None:
    runner, _, _ = make_harness(tmp_path)
    results = runner.run()
    meta = ReportMeta(
        date="2026-08-08",
        android="n/a",
        build="n/a",
        backend_url="http://0.0.0.0:8000",
        phone="n/a",
        resolution="n/a",
        verified=False,
    )

    report = render_report(results, meta)

    assert "⚠️ unverified — no device available" in report


# --------------------------------------------------------------------------
# (b) one logcat-error match flips a step from ✅ to ❌
# --------------------------------------------------------------------------


def test_logcat_error_flips_step_to_fail(tmp_path: Path) -> None:
    runner, _, _ = make_harness(tmp_path)
    results = runner.run()
    assert all(r.ok for r in results)

    # Script a logcat dump whose open_view_newest window (after its own
    # STEP_<n> marker, before the next) carries a decode error.
    names = [r.name for r in results]
    error_step = names.index("open_view_newest") + 1
    dump: list[str] = []
    for index, result in enumerate(results, start=1):
        dump.append(f"08-08 10:00:00.000 I SWITCHFIN_TEST: STEP_{index}_{result.name}")
        if index == error_step:
            dump.append("08-08 10:00:00.050 E SWITCHFIN_TEST: nlohmann json exception")

    filtered = apply_logcat_filter(results, dump)
    by_name = {r.name: r for r in filtered}
    assert by_name["open_view_newest"].ok is False
    assert by_name["open_view_newest"].logcat_hits
    assert by_name["login"].ok
    assert by_name["views"].ok
    assert by_name["open_first_card_newest"].ok


# --------------------------------------------------------------------------
# (c) a timing-out step flips to ❌ and "skipped" propagates to the rest of
#     its view, while other views keep running
# --------------------------------------------------------------------------


def test_timeout_skips_remaining_steps_of_the_view(tmp_path: Path) -> None:
    # The movie row coordinate is calibrated, but tapping it produces no
    # request log line -> the step times out (not a "no calibration" fail).
    lines_no_movie = {k: v for k, v in FULL_TAP_LINES.items() if k != (400, 200)}
    runner, _, _ = make_harness(tmp_path, tap_lines=lines_no_movie)

    results = runner.run()
    by_name = {r.name: r for r in results}

    assert by_name["open_view_newest"].ok
    assert by_name["open_first_card_newest"].ok

    # the movie view's first step times out…
    assert by_name["open_view_movie"].ok is False
    assert by_name["open_view_movie"].timed_out
    # …and its remaining steps are marked skipped, not run
    assert by_name["open_first_card_movie"].skipped
    assert by_name["play_movie"].skipped

    # a hard failure means a non-zero exit
    assert run_exit_code(results) == 1


# --------------------------------------------------------------------------
# (d) a handshake failure exits non-zero without running the rest
# --------------------------------------------------------------------------


def test_handshake_failure_stops_the_run(tmp_path: Path) -> None:
    runner, _, _ = make_harness(tmp_path, issue_ok=False)

    results = runner.run()

    assert [r.name for r in results] == ["login"]
    assert results[0].ok is False
    assert run_exit_code(results) == 1


# --------------------------------------------------------------------------
# warmup phase (device-driving B1): prime each view's /Items cache
# --------------------------------------------------------------------------


def test_warmup_step_issues_view_items_request() -> None:
    step = Step(
        name="warmup_newest",
        phase="warmup",
        view=None,
        tap=None,
        expects=(),
        view_id="abc123",
        use_token=True,
    )
    assert issue_path(step, {"user_id": "u1"}) == "/Users/u1/Items?parentId=abc123"
    with pytest.raises(RuntimeError):
        issue_path(step, {})  # no user_id yet


def test_warmup_failure_keeps_the_run_going(tmp_path: Path) -> None:
    """A failed warmup records a verdict but must NOT abort the run.

    Warmup is a precondition, not a test: if a prime fails the phone still
    drives the view and the open step reports the real outcome. This pins
    the ``continue`` (vs handshake's ``break``) in Runner.run.
    """
    steps_yaml = STEPS_YAML.replace(
        '  - name: warmup_newest\n    phase: warmup\n    view_id: v_newest\n    use_token: true\n    expect:\n      - request: "GET (/Users/[^ ]+)?/Items -> 200"\n        status: 200\n',
        '  - name: warmup_newest\n    phase: warmup\n    view_id: v_newest\n    use_token: true\n    expect:\n      - request: "GET /NEVER -> 200"\n        status: 200\n',
    )
    assert "GET /NEVER" in steps_yaml  # the replace actually landed

    runner, _, _ = make_harness(tmp_path, steps_yaml=steps_yaml)
    results = runner.run()
    by_name = {r.name: r for r in results}

    assert by_name["warmup_newest"].ok is False
    assert by_name["warmup_newest"].timed_out is True
    assert by_name["open_view_newest"].ok is True  # the run continued
    assert run_exit_code(results) == 1  # the failed warmup marks the verdict


# --------------------------------------------------------------------------
# restart phase (device-driving B17/B18, ticket #208): relaunch the app
# between warmup and the first open step, wait for it to reconnect
# --------------------------------------------------------------------------


def test_splice_restart_step_inserts_after_last_warmup() -> None:
    def step(name: str, phase: str) -> Step:
        return Step(name=name, phase=phase, view=None, tap=None, expects=())

    steps = [
        step("login", "handshake"),
        step("views", "handshake"),
        step("warmup_a", "warmup"),
        step("warmup_b", "warmup"),
        step("open", "open"),
    ]
    spliced = splice_restart_step(steps)
    assert [s.name for s in spliced] == [
        "login",
        "views",
        "warmup_a",
        "warmup_b",
        "restart_app",
        "open",
    ]
    assert spliced[4].phase == "restart"
    # no warmup steps -> unchanged
    assert [s.name for s in splice_restart_step(steps[:2])] == ["login", "views"]


def test_restart_phase_relaunches_app_and_runs_open_after(tmp_path: Path) -> None:
    """Runner.run relaunches the app on the restart step and keeps driving."""
    steps_yaml = STEPS_YAML.replace(
        "  - name: open_view_newest\n",
        "  - name: restart_app\n    phase: restart\n"
        "  - name: open_view_newest\n",
    )
    assert "phase: restart" in steps_yaml

    runner, _, adb = make_harness(tmp_path, steps_yaml=steps_yaml)
    results = runner.run()
    by_name = {r.name: r for r in results}

    assert adb.restarts == 1
    assert by_name["restart_app"].ok is True
    assert by_name["open_view_newest"].ok is True  # drove after the relaunch


def test_restart_skipped_without_device(tmp_path: Path) -> None:
    """No device -> the restart step is skipped, not failed."""
    steps_yaml = STEPS_YAML.replace(
        "  - name: open_view_newest\n",
        "  - name: restart_app\n    phase: restart\n"
        "  - name: open_view_newest\n",
    )
    adb = FakeAdb(lines=[], tap_lines={})
    adb._available = False
    runner, _, adb = make_harness(tmp_path, steps_yaml=steps_yaml, adb=adb)

    results = runner.run()
    restart = next(r for r in results if r.name == "restart_app")

    assert restart.ok is True
    assert restart.skipped is True
    assert adb.restarts == 0


# --------------------------------------------------------------------------
# B22: play steps tolerate the app's ~5s playback-start latency
# --------------------------------------------------------------------------


def test_play_step_tolerates_delayed_sessions_playing(tmp_path: Path) -> None:
    """Sessions/Playing arrives seconds after the tap — the play step must
    keep its expect window open long enough to catch it (device-driving B22:
    play_newest fired PlaybackInfo + stream + segments but its
    Sessions/Playing landed just past the 8s step deadline).
    """
    tap_lines = {k: list(v) for k, v in FULL_TAP_LINES.items()}
    # the play_button tap fires PlaybackInfo + stream immediately, but
    # Sessions/Playing only arrives after a delay (the app's buffer
    # latency) — a repeating timer feeds every play step its own Playing
    tap_lines[TAPS["play_button"]] = [
        "POST /Items/g1/PlaybackInfo -> 200 (0ms)",
        "GET /Videos/g1/stream -> 302 (0ms)",
    ]
    adb = FakeAdb(lines=[], tap_lines=tap_lines)
    stop = threading.Event()

    def feed_playing() -> None:
        while not stop.is_set():
            adb._lines.append("POST /Sessions/Playing -> 204 (0ms)")
            time.sleep(1.0)

    feeder = threading.Thread(target=feed_playing, daemon=True)
    feeder.start()
    try:
        runner, _, _ = make_harness(tmp_path, adb=adb, play_timeout_s=30.0)
        results = runner.run()
    finally:
        stop.set()
        feeder.join(timeout=2)

    by_name = {r.name: r for r in results}
    # both movie play steps land all three expects: PlaybackInfo + stream
    # immediately on tap, Sessions/Playing up to a second later
    assert by_name["play_newest"].ok is True
    assert by_name["play_movie"].ok is True


# --------------------------------------------------------------------------
# #210/B13: warmup primes the first card's detail + series play chain
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_warm_view_details_warms_movie_detail_only() -> None:
    """A Movie first card warms grid + detail, and stops there."""
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            {"Items": [{"Id": "g2:m1", "Type": "Movie"}]},
            {"Id": "g2:m1", "Type": "Movie"},
        ]
    )

    def fake_urlopen(req: object, timeout: float = 10) -> _FakeResponse:
        method = req.get_method() if hasattr(req, "get_method") else "GET"  # type: ignore[attr-defined]
        url = req.full_url if hasattr(req, "full_url") else str(req)  # type: ignore[attr-defined]
        calls.append((method, url))
        return _FakeResponse(json.dumps(next(responses)).encode())

    step = Step(
        name="warmup_movie",
        phase="warmup",
        view=None,
        tap=None,
        expects=(),
        view_id="v_movie",
        use_token=True,
    )
    runner = Runner(
        [step],
        {},
        FakeTailer([]),
        FakeAdb(lines=[], tap_lines={}),
        host="127.0.0.1",
        port=1,
        timeout_s=8.0,
    )
    runner._ctx["user_id"] = "u1"
    runner._ctx["token"] = "tok"

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        runner._warm_view_details(step)

    assert [m for m, _ in calls] == ["GET", "GET"]
    assert "Items?parentId=v_movie" in calls[0][1]
    assert "/Items/g2%3Am1" in calls[1][1]


def test_warm_view_details_warms_series_play_chain() -> None:
    """A Series first card warms detail -> Seasons -> Episodes -> PlaybackInfo."""
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            {"Items": [{"Id": "g2:s1", "Type": "Series"}]},
            {"Id": "g2:s1", "Type": "Series"},
            {"Items": [{"Id": "g2:s1:S1", "Name": "Сезон 1"}]},
            {"Items": [{"Id": "g2:s1:e1", "Name": "Серія 1"}]},
            {"MediaSources": []},
        ]
    )

    def fake_urlopen(req: object, timeout: float = 10) -> _FakeResponse:
        method = req.get_method() if hasattr(req, "get_method") else "GET"  # type: ignore[attr-defined]
        url = req.full_url if hasattr(req, "full_url") else str(req)  # type: ignore[attr-defined]
        calls.append((method, url))
        return _FakeResponse(json.dumps(next(responses)).encode())

    step = Step(
        name="warmup_series",
        phase="warmup",
        view=None,
        tap=None,
        expects=(),
        view_id="v_series",
        use_token=True,
    )
    runner = Runner(
        [step],
        {},
        FakeTailer([]),
        FakeAdb(lines=[], tap_lines={}),
        host="127.0.0.1",
        port=1,
        timeout_s=8.0,
    )
    runner._ctx["user_id"] = "u1"
    runner._ctx["token"] = "tok"

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        runner._warm_view_details(step)

    assert [m for m, _ in calls] == ["GET", "GET", "GET", "GET", "POST"]
    assert "/Items/g2%3As1" in calls[1][1]
    assert "/Shows/g2%3As1/Seasons" in calls[2][1]
    assert "/Shows/g2%3As1/Episodes?seasonId=g2%3As1%3AS1" in calls[3][1]
    assert calls[4][0] == "POST" and "/Items/g2%3As1%3Ae1/PlaybackInfo" in calls[4][1]


# --------------------------------------------------------------------------
# #149: every ❌ step writes a logcat snapshot, even with an empty window
# --------------------------------------------------------------------------


EMPTY_WINDOW_NOTE = "no logcat lines in this step window"


def _failed_result(
    name: str, logcat_window: tuple[str, ...] = ()
) -> StepResult:
    return StepResult(
        name=name,
        phase="play",
        view="Фільми",
        ok=False,
        timed_out=True,
        logcat_window=logcat_window,
    )


def test_failed_step_writes_logcat_snapshot(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    write_snapshots(
        [_failed_result("play_movie", logcat_window=("line a", "line b"))],
        artifacts,
    )

    written = (artifacts / "logcat-play_movie.txt").read_text(encoding="utf-8")
    assert written.splitlines() == ["line a", "line b"]


def test_failed_step_with_empty_window_still_writes_snapshot(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    write_snapshots([_failed_result("play_movie")], artifacts)

    written = (artifacts / "logcat-play_movie.txt").read_text(encoding="utf-8")
    assert EMPTY_WINDOW_NOTE in written


def test_ok_and_skipped_steps_write_no_snapshots(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"

    write_snapshots(
        [
            StepResult(name="login", phase="handshake", view=None, ok=True),
            StepResult(
                name="play_movie",
                phase="play",
                view="Фільми",
                ok=False,
                skipped=True,
            ),
        ],
        artifacts,
    )

    assert not artifacts.exists()


# --------------------------------------------------------------------------
# type-aware play sequencing (#146)
# --------------------------------------------------------------------------


def test_series_play_taps_two_in_order(tmp_path: Path) -> None:
    runner, _, adb = make_harness(tmp_path, probe="Series")
    results = runner.run()

    play = next(r for r in results if r.name == "play_newest")
    assert play.ok, play.note

    # device-driving B7: the episode-row tap auto-plays on the real client,
    # so the series branch is first_season -> first_episode (no separate
    # seasons_tab / play_button taps).
    expected = [
        TAPS["first_season"],
        TAPS["first_episode"],
    ]
    assert adb.taps[-2:] == expected


def test_movie_play_taps_once(tmp_path: Path) -> None:
    runner, _, adb = make_harness(tmp_path, probe="Movie")
    results = runner.run()

    play = next(r for r in results if r.name == "play_newest")
    assert play.ok, play.note
    assert adb.taps[-1:] == [TAPS["play_button"]]


def _teal_pill_png(size: tuple[int, int], pill: tuple[int, int, int, int]) -> bytes:
    """A plain gray frame with a teal rectangle (a synthetic detail screen)."""
    import io

    from PIL import Image, ImageDraw  # type: ignore[import-untyped]

    img = Image.new("RGB", size, (220, 220, 220))
    ImageDraw.Draw(img).rectangle(pill, fill=(0, 170, 210))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_find_play_pill_locates_teal_pill() -> None:
    """#202: the pill scan returns the teal rectangle's center (device-driving
    B10 — the fixed coordinate misses because the pill's y varies per item)."""
    center = find_play_pill(_teal_pill_png((1600, 900), (700, 400, 970, 500)))
    assert center is not None
    assert center[0] == 835  # (700 + 970) // 2
    assert abs(center[1] - 450) <= 2  # row scan steps of 2px


def test_find_play_pill_absent_on_plain_frame() -> None:
    assert find_play_pill(_teal_pill_png((1600, 900), (0, 0, 0, 0))) is None


def test_play_button_retries_until_located(tmp_path: Path) -> None:
    """The Movie branch locates the pill and retries when the first attempt
    lands before the detail has rendered (B10 timing race)."""
    steps_yaml = STEPS_YAML.replace("timeout_s: 0.4", "timeout_s: 3.0")
    attempts: list[int] = []

    def locator() -> tuple[int, int] | None:
        attempts.append(1)
        return None if len(attempts) == 1 else (825, 470)

    tap_lines = {k: list(v) for k, v in FULL_TAP_LINES.items()}
    tap_lines[(500, 400)] = []  # calibrated spot: pill not rendered yet
    tap_lines[(825, 470)] = FULL_TAP_LINES[(500, 400)]  # pill: playback fires

    runner, _, adb = make_harness(
        tmp_path,
        steps_yaml=steps_yaml,
        tap_lines=tap_lines,
        play_locator=locator,
    )
    results = runner.run()

    play = next(r for r in results if r.name == "play_newest")
    assert play.ok, play.note
    assert len(attempts) >= 2, "the locator must be retried"
    assert (825, 470) in adb.taps


def test_play_button_fails_cleanly_when_pill_never_found(tmp_path: Path) -> None:
    """No pill and no PlaybackInfo -> the play step ❌ but the run continues
    (other views still run; the failure is recorded, not a crash)."""
    steps_yaml = STEPS_YAML.replace("timeout_s: 0.4", "timeout_s: 0.6")
    tap_lines = {k: list(v) for k, v in FULL_TAP_LINES.items()}
    tap_lines[(500, 400)] = []

    runner, _, _ = make_harness(
        tmp_path,
        steps_yaml=steps_yaml,
        tap_lines=tap_lines,
        play_locator=lambda: None,
    )
    results = runner.run()

    play = next(r for r in results if r.name == "play_newest")
    assert play.ok is False
    assert "play_button" in play.note
    movie_open = next(r for r in results if r.name == "open_view_movie")
    assert movie_open.ok, movie_open.note  # the run continued


def test_nav_step_presses_back_between_views(tmp_path: Path) -> None:
    """A ``phase: nav`` step emits BACK presses and passes without requests
    (device-driving B6: the runner must navigate back to the grid between
    per-view blocks on the real client)."""
    runner, _, adb = make_harness(tmp_path, probe="Movie")
    results = runner.run()

    nav = next(r for r in results if r.name == "back_to_grid")
    assert nav.ok, nav.note
    assert nav.phase == "nav"
    assert adb.backs == 4

    # the views after the nav step still run normally
    movie_open = next(r for r in results if r.name == "open_view_movie")
    assert movie_open.ok, movie_open.note


# --------------------------------------------------------------------------
# shipped data files parse (guards the real steps.yaml / tap-coords.yaml)
# --------------------------------------------------------------------------


def test_shipped_steps_yaml_parses() -> None:
    steps_path = REPO_ROOT / "docs" / "test-artifacts" / "switchfin" / "steps.yaml"
    timeout_s, steps = load_steps(steps_path)

    assert timeout_s == 8
    # 2 handshake + 7 warmup + 7 × (open + detail + play + back_to_grid nav)
    assert len(steps) == 37
    assert sum(1 for s in steps if s.phase == "handshake") == 2
    assert sum(1 for s in steps if s.phase == "warmup") == 7
    assert sum(1 for s in steps if s.phase == "open") == 7
    assert sum(1 for s in steps if s.phase == "detail") == 7
    assert sum(1 for s in steps if s.phase == "play") == 7
    assert sum(1 for s in steps if s.phase == "nav") == 7
    for step in steps:
        if step.phase == "play":
            assert {branch.key for branch in step.branches} == {"movie", "series"}
        if step.phase == "nav":
            assert step.nav > 0  # device-driving B6: BACK to the grid

    # #151: each open_view step carries its uuid5 view id as a DATA field
    # (the router's `_VIEW_ID_BY_TYPE`), not as a comment. Read the raw YAML
    # because `load_steps` intentionally drops the unknown field.
    raw = yaml.safe_load(steps_path.read_text(encoding="utf-8")) or {}
    open_steps = [s for s in raw["steps"] if s["phase"] == "open"]
    assert len(open_steps) == 7
    for step in open_steps:
        view_id = step.get("view_id")
        assert isinstance(view_id, str) and len(view_id) == 32, (
            f"{step['name']}: view_id must be a data field (32-hex uuid5), "
            f"got {view_id!r}"
        )
        assert view_id == uuid.uuid5(
            uuid.NAMESPACE_URL, f"cs-uk-api-view:{step['view']}"
        ).hex, f"{step['name']}: view_id must match the router's derivation"


def test_shipped_tap_coords_load() -> None:
    taps_path = REPO_ROOT / "docs" / "test-artifacts" / "switchfin" / "tap-coords.yaml"
    coords = load_tap_coords(taps_path)

    assert set(coords) == set(CALIBRATION_ELEMENTS)
    # Calibrated on the OnePlus 8 Pro (2026-08-10): every element the runner
    # taps must have a real position (device-driving.md B8 table).
    assert all(x > 0 and y > 0 for key, (x, y) in coords.items() if key != "login_button")


def test_calibration_element_order_matches_definition() -> None:
    assert CALIBRATION_ELEMENTS == (
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


# --------------------------------------------------------------------------
# review regression: poster line must not poison the gk capture (#143 review-2)
# --------------------------------------------------------------------------


def test_poster_line_first_does_not_poison_gk(tmp_path: Path) -> None:
    """If the poster request lands before the detail request, the gk capture
    regex must NOT swallow the ``/Images/Primary`` suffix into the group key
    (the poisoned key 404s the play step's Type probe on a healthy backend)."""
    tap_lines = {k: list(v) for k, v in FULL_TAP_LINES.items()}
    tap_lines[(500, 300)] = [
        "GET /Items/g1/Images/Primary -> 200 (0ms)",  # poster lands first
        "GET /Users/u1/Items/g1 -> 200 (0ms)",  # then the detail fetch
    ]
    seen_gk: list[str] = []

    def probe(ctx: dict[str, str]) -> str | None:
        seen_gk.append(str(ctx.get("gk")))
        return "Movie"

    runner, _, _ = make_harness(tmp_path, tap_lines=tap_lines, probe_fn=probe)
    results = runner.run()

    assert all(r.ok for r in results), [r.note for r in results if not r.ok]
    assert seen_gk, "the play step's Type probe never ran"
    assert (
        seen_gk[0] == "g1"
    ), f"gk captured as {seen_gk[0]!r} — poster suffix leaked in"


# --------------------------------------------------------------------------
# review regression: a dropped logcat marker must not shift windows (#143 review-4)
# --------------------------------------------------------------------------


def test_missing_marker_does_not_cascade_windows(tmp_path: Path) -> None:
    """When a STEP_<n> marker is missing from the logcat dump, the step's
    window must not be silently widened to another step's (or the whole dump's)
    lines — that would attribute a neighbour's error and flip a false ❌."""
    runner, _, _ = make_harness(tmp_path)
    results = runner.run()
    assert all(r.ok for r in results)

    names = [r.name for r in results]
    missing_step = names.index("open_view_newest") + 1
    error_step = names.index("open_first_card_newest") + 1
    dump: list[str] = []
    for index, result in enumerate(results, start=1):
        if index == missing_step:  # open_view_newest's marker is evicted
            continue
        dump.append(f"08-08 10:00:00.000 I SWITCHFIN_TEST: STEP_{index}_{result.name}")
        if index == error_step:
            dump.append("08-08 10:00:00.050 E SWITCHFIN_TEST: nlohmann json exception")

    filtered = apply_logcat_filter(results, dump)
    by_name = {r.name: r for r in filtered}
    # open_view_newest has no marker -> verdict untouched, no whole-dump attribution
    assert by_name["open_view_newest"].ok
    assert not by_name["open_view_newest"].logcat_hits
    # open_first_card_newest's own window still catches its error
    assert by_name["open_first_card_newest"].ok is False
    assert by_name["open_first_card_newest"].logcat_hits


# --------------------------------------------------------------------------
# review regression: getevent sampling must be bounded (#143 review-1/7)
# --------------------------------------------------------------------------


def test_read_getevent_bounds_when_no_input(monkeypatch: object) -> None:
    """read_getevent must return after the sample window even when the phone
    emits no input events — a blocking readline would hang calibration forever."""
    read_fd, write_fd = os.pipe()
    fake_stdout = os.fdopen(read_fd, "r", encoding="utf-8")

    class _FakeProc:
        stdout = fake_stdout

        def terminate(self) -> None:
            os.close(write_fd)

        def wait(self, timeout: float) -> None:
            return None

    import scripts.switchfin_adb as adb_mod

    monkeypatch.setattr(adb_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())
    adb = Adb(binary="fake-adb")

    start = time.monotonic()
    x, y = read_getevent(adb, duration=0.2)
    elapsed = time.monotonic() - start

    assert (x, y) == (0, 0)
    assert elapsed < 2.0, f"read_getevent blocked for {elapsed:.1f}s with no input"


# --------------------------------------------------------------------------
# review regression: a mid-run adb tap failure must not crash the run (#143 review-6)
# --------------------------------------------------------------------------


class _ExplodingAdb(FakeAdb):
    """FakeAdb whose Nth tap raises (simulates the device vanishing mid-run)."""

    def __init__(
        self,
        lines: list[str],
        tap_lines: dict[tuple[int, int], list[str]],
        fail_on: int,
    ) -> None:
        super().__init__(lines=lines, tap_lines=tap_lines)
        self._fail_on = fail_on
        self._taps_so_far = 0

    def tap(self, x: int, y: int) -> None:
        self._taps_so_far += 1
        if self._taps_so_far == self._fail_on:
            raise subprocess.CalledProcessError(1, ["adb", "shell", "input", "tap"])
        super().tap(x, y)


def test_tap_failure_records_fail_instead_of_crashing(tmp_path: Path) -> None:
    exploding = _ExplodingAdb(lines=[], tap_lines=FULL_TAP_LINES, fail_on=1)
    runner, _, _ = make_harness(tmp_path, adb=exploding)
    results = runner.run()
    by_name = {r.name: r for r in results}

    assert by_name["open_view_newest"].ok is False
    assert "adb tap failed" in by_name["open_view_newest"].note
    # the rest of the failed view is skipped, other views still run
    assert by_name["open_first_card_newest"].skipped
    assert by_name["play_newest"].skipped
    assert by_name["open_view_movie"].ok
    assert run_exit_code(results) == 1


# --------------------------------------------------------------------------
# capture fixture slicing (#147)
# --------------------------------------------------------------------------


def _capture_line(
    ts: float, method: str = "GET", path: str = "/System/Info/Public"
) -> str:
    record: dict[str, object] = {
        "ts": ts,
        "method": method,
        "path": path,
        "query": {},
        "headers": {},
        "status": 200,
    }
    return json.dumps(record)


def test_capture_in_window_predicate() -> None:
    record = json.loads(_capture_line(150.0))
    assert capture_in_window(record, CaptureWindow(100.0, 200.0))
    assert not capture_in_window(record, CaptureWindow(160.0, 200.0))
    assert not capture_in_window(record, CaptureWindow(100.0, 140.0))
    # non-numeric / missing ts fields are out of window, never a crash
    assert not capture_in_window({}, CaptureWindow(100.0, 200.0))
    assert not capture_in_window({"ts": "not-a-number"}, CaptureWindow(100.0, 200.0))


def test_splice_capture_keeps_only_in_window_records(tmp_path: Path) -> None:
    capture = tmp_path / "capture.jsonl"
    out = tmp_path / "capture.real-client.jsonl"
    lines = [
        _capture_line(100.0),
        _capture_line(150.0),
        _capture_line(200.0),
        _capture_line(250.0),
    ]
    capture.write_text("\n".join(lines) + "\n", encoding="utf-8")

    kept = splice_capture(capture, out, CaptureWindow(120.0, 220.0))

    assert kept == 2
    written = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["ts"] for line in written] == [150.0, 200.0]
    # the working capture the backend appended to is never modified
    assert capture.read_text(encoding="utf-8").splitlines() == lines


def test_splice_capture_is_inclusive_of_both_bounds(tmp_path: Path) -> None:
    capture = tmp_path / "capture.jsonl"
    out = tmp_path / "capture.real-client.jsonl"
    capture.write_text(
        "\n".join([_capture_line(100.0), _capture_line(200.0), _capture_line(300.0)])
        + "\n",
        encoding="utf-8",
    )

    kept = splice_capture(capture, out, CaptureWindow(100.0, 300.0))

    assert kept == 3


def test_splice_capture_skips_malformed_lines(tmp_path: Path) -> None:
    capture = tmp_path / "capture.jsonl"
    out = tmp_path / "capture.real-client.jsonl"
    # a partial line from a mid-write append must not crash the splice
    capture.write_text(
        _capture_line(150.0) + "\n{truncated-json\n" + _capture_line(250.0) + "\n",
        encoding="utf-8",
    )

    kept = splice_capture(capture, out, CaptureWindow(100.0, 300.0))

    assert kept == 2


def test_splice_capture_missing_source_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "capture.real-client.jsonl"

    kept = splice_capture(tmp_path / "missing.jsonl", out, CaptureWindow(0.0, 1.0))

    assert kept == 0
    assert not out.exists()


def test_splice_capture_no_matches_leaves_fixture_untouched(tmp_path: Path) -> None:
    """A window matching nothing (a degenerate or failed run) must not clobber
    an existing good fixture to empty — a FAIL run keeps the last capture."""
    capture = tmp_path / "capture.jsonl"
    out = tmp_path / "capture.real-client.jsonl"
    capture.write_text(_capture_line(100.0) + "\n", encoding="utf-8")
    out.write_text(_capture_line(999.0) + "\n", encoding="utf-8")

    kept = splice_capture(capture, out, CaptureWindow(200.0, 300.0))

    assert kept == 0
    assert out.read_text(encoding="utf-8").splitlines() == [_capture_line(999.0)]


def test_reset_capture_dir_empties_existing_dir(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    stale = capture_dir / "capture.jsonl"
    stale.write_text("stale\n", encoding="utf-8")

    reset_capture_dir(capture_dir)

    assert capture_dir.is_dir()
    assert not stale.exists()
    assert list(capture_dir.iterdir()) == []


def test_reset_capture_dir_creates_missing_dir(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"

    reset_capture_dir(capture_dir)

    assert capture_dir.is_dir()


# --------------------------------------------------------------------------
# #148: the shipped series-play expects match the real Switchfin client
# --------------------------------------------------------------------------


def test_shipped_series_play_patterns_match_real_client_lines() -> None:
    """#148 + device-driving B7: the shipped series-play expects must match
    the bare access-log paths the real Switchfin client emits. The seasons
    rail is ``/Shows/{series}/Seasons`` (``apiShowSeasons``) — fired on the
    detail open, so no ``seasons_tab`` tap exists; the ``first_season`` tap
    fires ``/Shows/{series}/Episodes`` (``apiShowEpisodes``) and the
    ``first_episode`` row tap AUTO-PLAYS (PlaybackInfo + stream + Sessions).
    Verified on-device 2026-08-10 (see device-driving.md B7). The middleware
    strips the query string, so an SDK-style call would log as a bare
    ``GET /Items`` and the step would time out against a real device. The
    reference lines below encode that verified shape; a future "correction"
    back to the spec's SDK spelling must fail this pin."""
    steps_path = REPO_ROOT / "docs" / "test-artifacts" / "switchfin" / "steps.yaml"
    _, steps = load_steps(steps_path)
    play_series = next(
        (s for s in steps if s.name == "play_series"),
        None,
    )
    assert play_series is not None, "steps.yaml is missing the play_series step"
    series_branch = next(
        (b for b in play_series.branches if b.key == "series"),
        None,
    )
    assert series_branch is not None, "play_series is missing its series branch"

    # Bare access-log lines in the shape the real client emits (verified
    # on-device). Deliberately query-free: the middleware logs
    # `request.url.path`.
    real_client_lines = {
        "first_season": "GET /Shows/g1/Episodes -> 200 (5ms)",
        "first_episode": "POST /Items/e1/PlaybackInfo -> 200 (5ms)",
    }
    # Assert both pinned taps exist so a steps.yaml rename fails loudly here,
    # not silently (the loop below would otherwise skip and pass on nothing).
    tap_names = {tap.tap for tap in series_branch.taps}
    missing = set(real_client_lines) - tap_names
    assert not missing, f"steps.yaml dropped pinned series taps: {missing!r}"
    for tap in series_branch.taps:
        real_line = real_client_lines.get(tap.tap)
        if real_line is None:
            continue
        patterns = [e.request for e in tap.expects]
        assert patterns, f"{tap.tap} tap must expect the rail request"
        assert any(re.search(p, real_line) for p in patterns), (
            f"{tap.tap}: no expect matches the real client's {real_line!r} "
            f"(patterns: {patterns!r})"
        )
