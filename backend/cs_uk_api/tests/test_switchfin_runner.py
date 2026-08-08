"""Unit tests for the Switchfin manual-test runner (tickets #144-#146).

Runs inside the backend pytest suite, but exercises ONLY the runner's
parsing/sequencing logic — ADB subprocess calls and the backend request
log are replaced by scripted fakes, so nothing touches a device or an
HTTP server. The backend conftest only puts ``backend/`` on ``sys.path``;
this file adds the repo root so ``scripts.switchfin_test`` imports.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # type: ignore[import-untyped]
from scripts.switchfin_adb import Adb, LogTailer  # type: ignore[import-not-found]
from scripts.switchfin_model import (  # type: ignore[import-not-found]
    CALIBRATION_ELEMENTS,
    ReportMeta,
    Step,
)
from scripts.switchfin_report import (  # type: ignore[import-not-found]
    apply_logcat_filter,
    render_report,
    run_exit_code,
)
from scripts.switchfin_test import (  # type: ignore[import-not-found]
    Runner,
    load_steps,
    load_tap_coords,
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
        self.markers: list[str] = []
        self._available = True

    def available(self) -> bool:
        return self._available

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        self._lines.extend(self._tap_lines.get((x, y), []))

    def marker(self, text: str) -> None:
        self.markers.append(text)

    def logcat_dump(self) -> list[str]:
        return []

    def shell(self, command: str) -> str:
        return "n/a"


# --------------------------------------------------------------------------
# fixture data (mirrors the shipped steps.yaml shape, two views)
# --------------------------------------------------------------------------

STEPS_YAML = """\
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
      - request: "GET (/Users/[^ ]+)?/Items/(?P<gk>[^ ]+) -> 200"
        status: 200
        capture: gk
      - request: "GET /Items/[^ ]+/Images/Primary -> 200"
        status: 200
  - name: play_newest
    phase: play
    view: newest
    branches:
      movie:
        - tap: play_button
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|302)"
              status: [200, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
      series:
        - tap: seasons_tab
          expect:
            - request: "GET /Shows/[^ ]+/Seasons -> 200"
              status: 200
        - tap: first_season
          expect:
            - request: "GET /Shows/[^ ]+/Episodes -> 200"
              status: 200
        - tap: first_episode
        - tap: play_button
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|302)"
              status: [200, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
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
      - request: "GET (/Users/[^ ]+)?/Items/(?P<gk>[^ ]+) -> 200"
        status: 200
        capture: gk
      - request: "GET /Items/[^ ]+/Images/Primary -> 200"
        status: 200
  - name: play_movie
    phase: play
    view: movie
    branches:
      movie:
        - tap: play_button
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|302)"
              status: [200, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
      series:
        - tap: seasons_tab
          expect:
            - request: "GET /Shows/[^ ]+/Seasons -> 200"
              status: 200
        - tap: first_season
          expect:
            - request: "GET /Shows/[^ ]+/Episodes -> 200"
              status: 200
        - tap: first_episode
        - tap: play_button
          expect:
            - request: "POST /Items/[^ ]+/PlaybackInfo -> 200"
              status: 200
            - request: "GET /Videos/[^ ]+/stream -> (200|302)"
              status: [200, 302]
            - request: "POST /Sessions/Playing -> 204"
              status: 204
"""

TAPS: dict[str, tuple[int, int]] = {
    "view_newest_x": (100, 200),
    "view_movie_x": (400, 200),
    "first_card": (500, 300),
    "play_button": (500, 400),
    "seasons_tab": (500, 450),
    "first_season": (500, 500),
    "first_episode": (500, 550),
}

FULL_TAP_LINES: dict[tuple[int, int], list[str]] = {
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
    (500, 450): ["GET /Shows/g1/Seasons -> 200 (0ms)"],
    (500, 500): ["GET /Shows/g1/Episodes -> 200 (0ms)"],
    (500, 550): ["GET /Users/u1/Items/e1 -> 200 (0ms)"],
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
    lines: list[str] = []
    tailer = FakeTailer(lines)
    adb = FakeAdb(
        lines=lines, tap_lines=FULL_TAP_LINES if tap_lines is None else tap_lines
    )

    def issue(step: Step, ctx: dict[str, str]) -> None:
        if not issue_ok:
            return
        assert step.method is not None and step.path is not None
        lines.append(f"{step.method} {step.path} -> 200 (0ms)")
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
        probe_fn=lambda _ctx: probe,
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

    assert len(results) == 8
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
# (b) one logcat-error match flips a step from ✅ to ❌
# --------------------------------------------------------------------------


def test_logcat_error_flips_step_to_fail(tmp_path: Path) -> None:
    runner, _, _ = make_harness(tmp_path)
    results = runner.run()
    assert all(r.ok for r in results)

    # Script a logcat dump whose step-3 window (after its STEP_3 marker,
    # before STEP_4) carries a decode error.
    dump: list[str] = []
    for index, result in enumerate(results, start=1):
        dump.append(f"08-08 10:00:00.000 I SWITCHFIN_TEST: STEP_{index}_{result.name}")
        if index == 3:
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
# type-aware play sequencing (#146)
# --------------------------------------------------------------------------


def test_series_play_taps_four_in_order(tmp_path: Path) -> None:
    runner, _, adb = make_harness(tmp_path, probe="Series")
    results = runner.run()

    play = next(r for r in results if r.name == "play_newest")
    assert play.ok, play.note

    expected = [
        TAPS["seasons_tab"],
        TAPS["first_season"],
        TAPS["first_episode"],
        TAPS["play_button"],
    ]
    assert adb.taps[-4:] == expected


def test_movie_play_taps_once(tmp_path: Path) -> None:
    runner, _, adb = make_harness(tmp_path, probe="Movie")
    results = runner.run()

    play = next(r for r in results if r.name == "play_newest")
    assert play.ok, play.note
    assert adb.taps[-1:] == [TAPS["play_button"]]


# --------------------------------------------------------------------------
# shipped data files parse (guards the real steps.yaml / tap-coords.yaml)
# --------------------------------------------------------------------------


def test_shipped_steps_yaml_parses() -> None:
    steps_path = REPO_ROOT / "docs" / "test-artifacts" / "switchfin" / "steps.yaml"
    timeout_s, steps = load_steps(steps_path)

    assert timeout_s == 8
    assert len(steps) == 23  # 2 handshake + 7 × (open + detail + play)
    assert sum(1 for s in steps if s.phase == "handshake") == 2
    assert sum(1 for s in steps if s.phase == "open") == 7
    assert sum(1 for s in steps if s.phase == "detail") == 7
    assert sum(1 for s in steps if s.phase == "play") == 7
    for step in steps:
        if step.phase == "play":
            assert {branch.key for branch in step.branches} == {"movie", "series"}


def test_shipped_tap_coords_load() -> None:
    taps_path = REPO_ROOT / "docs" / "test-artifacts" / "switchfin" / "tap-coords.yaml"
    coords = load_tap_coords(taps_path)

    assert set(coords) == set(CALIBRATION_ELEMENTS)
    # the placeholder is all zeros, so the runner treats it as uncalibrated
    assert all(x == 0 and y == 0 for x, y in coords.values())


def test_calibration_element_order_matches_definition() -> None:
    assert CALIBRATION_ELEMENTS == (
        "login_button",
        "view_newest_x",
        "view_popular_x",
        "view_movie_x",
        "view_series_x",
        "view_anime_x",
        "view_cartoon_x",
        "view_dorama_x",
        "first_card",
        "play_button",
        "seasons_tab",
        "first_season",
        "first_episode",
    )
