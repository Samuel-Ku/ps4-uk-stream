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
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

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
)
from scripts.switchfin_report import (  # type: ignore[import-not-found]
    apply_logcat_filter,
    render_report,
    run_exit_code,
)
from scripts.switchfin_test import (  # type: ignore[import-not-found]
    Runner,
    capture_in_window,
    load_steps,
    load_tap_coords,
    reset_capture_dir,
    splice_capture,
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
      - request: "GET (/Users/[^ ]+)?/Items/(?P<gk>[^ /]+) -> 200"
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
      - request: "GET (/Users/[^ ]+)?/Items/(?P<gk>[^ /]+) -> 200"
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
    adb: FakeAdb | None = None,
    probe_fn: Callable[[dict[str, str]], str | None] | None = None,
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
        probe_fn=probe_fn or (lambda _ctx: probe),
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

    dump: list[str] = []
    for index, result in enumerate(results, start=1):
        if index == 3:  # open_view_newest's marker is evicted
            continue
        dump.append(f"08-08 10:00:00.000 I SWITCHFIN_TEST: STEP_{index}_{result.name}")
        if index == 4:
            dump.append("08-08 10:00:00.050 E SWITCHFIN_TEST: nlohmann json exception")

    filtered = apply_logcat_filter(results, dump)
    by_name = {r.name: r for r in filtered}
    # step 3 has no marker -> verdict untouched, no whole-dump attribution
    assert by_name["open_view_newest"].ok
    assert not by_name["open_view_newest"].logcat_hits
    # step 4's own window still catches its error
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
    """#148: the ``seasons_tab``/``first_season`` expects must match the bare
    access-log paths the real Switchfin client emits — ``/Shows/{series}/Seasons``
    and ``/Shows/{series}/Episodes``, its ``apiShowSeasons``/``apiShowEpisodes``
    constants in ``app/include/api/jellyfin/media.hpp`` (Switchfin source,
    branch dev) — NOT the JS-SDK ``/Items?parentId=…`` spelling. The middleware
    strips the query string, so an SDK-style call would log as a bare
    ``GET /Items`` and the step would time out against a real device. The
    reference lines below encode that source-derived shape; a future
    "correction" back to the spec's SDK spelling must fail this pin."""
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

    # Bare access-log lines in the shape the real client emits (source-derived
    # above). Deliberately query-free: the middleware logs `request.url.path`.
    real_client_lines = {
        "seasons_tab": "GET /Shows/g1/Seasons -> 200 (5ms)",
        "first_season": "GET /Shows/g1/Episodes -> 200 (5ms)",
    }
    # Assert both pinned taps exist so a steps.yaml rename fails loudly here,
    # not silently (the loop below would otherwise skip and pass on nothing).
    tap_names = {tap.tap for tap in series_branch.taps}
    missing = set(real_client_lines) - tap_names
    assert not missing, f"steps.yaml dropped pinned series taps: {missing!r}"
    for tap in series_branch.taps:
        real_line = real_client_lines.get(tap.tap)
        if real_line is None:
            # first_episode has no expects; play_button's playback trio is
            # orthogonal to the endpoint question #148 resolved.
            continue
        patterns = [e.request for e in tap.expects]
        assert patterns, f"{tap.tap} tap must expect the rail request"
        assert any(re.search(p, real_line) for p in patterns), (
            f"{tap.tap}: no expect matches the real client's {real_line!r} "
            f"(patterns: {patterns!r})"
        )
