#!/usr/bin/env python3
"""ADB-driven manual test runner for the Switchfin facade (tickets #144-#146).

Parent design: #143. The runner treats the uvicorn backend as a black box
and verifies it through the *wire*: cold-start the backend, then watch the
``cs_uk_api`` request-log middleware lines (``METHOD path -> status (ms)``)
as a real Switchfin client (or the runner's own handshake) drives it.

Three sequential slices:

  * #144 — skeleton: cold-start uvicorn, tail ``backend.log``, run the 2
    handshake steps (login + views) by issuing the requests itself.
  * #145 — ``--calibrate``: interactively record tap coordinates into
    ``tap-coords.yaml``, then drive the on-phone client with
    ``adb shell input tap`` to open all 7 library views and the first card
    of each. Per-view verdicts; a failed step skips the rest of its view.
  * #146 — type-aware play (Movie = 1 tap, Series = 4 taps), a logcat
    error filter per step window, and ``docs/switchfin-test-report.md``.
  * #147 — capture: cold-start the backend with ``CS_UK_JF_CAPTURE_DIR``
    set, empty the working capture dir, then slice the run's real-client
    records into ``backend/cs_uk_api/tests/fixtures/jellyfin/
    capture.real-client.jsonl`` (never touching ``capture.jsonl``).

The runner deliberately does NOT import ``cs_uk_api`` — only ``pyyaml``
plus the Python stdlib. Everything the backend knows is learned through
its request log; everything the phone knows is learned through logcat.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

# Direct invocation (`python3 scripts/switchfin_test.py`) puts only
# `scripts/` on sys.path; the split modules import as `scripts.*`, so the
# repo root must be added explicitly. `-m scripts.switchfin_test` already
# has it, so this bootstrap is a no-op there.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.switchfin_adb import Adb, LogTailer, calibrate
from scripts.switchfin_model import (
    Branch,
    CaptureWindow,
    Expect,
    PlayTap,
    ReportMeta,
    Step,
    StepResult,
    status_of,
)
from scripts.switchfin_report import (
    apply_logcat_filter,
    print_summary,
    render_report,
    run_exit_code,
    write_snapshots,
)

BACKEND_DIR = REPO_ROOT / "backend"
ARTIFACTS_DIR = REPO_ROOT / "docs" / "test-artifacts" / "switchfin"
DEFAULT_STEPS = ARTIFACTS_DIR / "steps.yaml"
DEFAULT_TAP_COORDS = ARTIFACTS_DIR / "tap-coords.yaml"
DEFAULT_BACKEND_LOG = ARTIFACTS_DIR / "backend.log"
DEFAULT_REPORT = REPO_ROOT / "docs" / "switchfin-test-report.md"

#: #147 capture plumbing. The backend subprocess runs with ``cwd=backend/``,
#: so the capture dir must be an absolute repo-root path — a bare
#: ``docs/test-artifacts/...`` would resolve under ``backend/`` and be
#: silently written to the wrong tree.
CAPTURE_DIR = REPO_ROOT / "docs" / "test-artifacts" / "switchfin" / "capture"
CAPTURE_JSONL = CAPTURE_DIR / "capture.jsonl"
FIXTURE_REAL_CLIENT = (
    REPO_ROOT
    / "backend"
    / "cs_uk_api"
    / "tests"
    / "fixtures"
    / "jellyfin"
    / "capture.real-client.jsonl"
)

#: HTTP timeout for the runner's own handshake requests. The first
#: ``/UserViews`` call builds the home cache by scraping every provider, so
#: a cold backend can take well over the 8s step-detection window to answer.
REQUEST_TIMEOUT_S = 120.0

#: Step-detection poll interval and item-Type probe timeout.
POLL_INTERVAL_S = 0.05
PROBE_TIMEOUT_S = 10.0

#: Readiness poll and backend-shutdown bounds.
READY_TIMEOUT_S = 30.0
SHUTDOWN_WAIT_S = 5.0


# --------------------------------------------------------------------------
# steps.yaml / tap-coords.yaml parsing (pure, unit-testable)
# --------------------------------------------------------------------------


def _expect(raw: dict[str, Any]) -> Expect:
    status = raw.get("status", 200)
    if isinstance(status, int):
        status = (status,)
    return Expect(
        request=raw["request"],
        status=tuple(int(s) for s in status),
        capture=raw.get("capture"),
    )


def _play_tap(raw: dict[str, Any]) -> PlayTap:
    return PlayTap(
        tap=raw["tap"],
        expects=tuple(_expect(e) for e in raw.get("expect", [])),
    )


def load_steps(path: Path) -> tuple[float, list[Step]]:
    """Parse steps.yaml into (timeout_s, [Step, ...]).

    The timeout is a top-level default, overridable via ``--timeout``.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    timeout_s = float(data.get("timeout_s", 8))
    steps: list[Step] = []
    for raw in data.get("steps", []):
        branches = tuple(
            Branch(key=key, taps=tuple(_play_tap(t) for t in taps))
            for key, taps in (raw.get("branches") or {}).items()
        )
        steps.append(
            Step(
                name=raw["name"],
                phase=raw["phase"],
                view=raw.get("view"),
                tap=raw.get("tap"),
                expects=tuple(_expect(e) for e in raw.get("expect", [])),
                branches=branches,
                method=raw.get("method"),
                path=raw.get("path"),
                body=raw.get("body"),
                capture_token=bool(raw.get("capture_token")),
                use_token=bool(raw.get("use_token")),
                view_id=raw.get("view_id"),
                nav=int(raw.get("nav") or 0),
            )
        )
    return timeout_s, steps


def issue_path(step: Step, ctx: dict[str, str]) -> str:
    """Resolve the HTTP path a runner-issued step should hit.

    Handshake steps carry an explicit ``path``. Warmup steps (B1) carry a
    ``view_id`` instead and are issued as the same library listing the
    phone's tap produces: ``GET /Users/{user}/Items?parentId={view_id}``.
    """
    if step.path:
        # ``Step`` is Any here when mypy can't resolve ``scripts.*`` (the
        # repo runs mypy on ``cs_uk_api`` only), so cast for the return type
        return cast(str, step.path)
    if step.view_id:
        user_id = ctx.get("user_id")
        if not user_id:
            raise RuntimeError(f"step {step.name} needs a user_id (login first)")
        return f"/Users/{user_id}/Items?parentId={step.view_id}"
    raise RuntimeError(f"step {step.name} has no path to issue")


def load_tap_coords(path: Path) -> dict[str, tuple[int, int]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: (int(v["x"]), int(v["y"])) for key, v in data.items()}


# --------------------------------------------------------------------------
# capture fixture slicing (#147)
# --------------------------------------------------------------------------


def capture_in_window(record: dict[str, object], window: CaptureWindow) -> bool:
    """True when a capture record's ``ts`` (host epoch) is within ``window``.

    Records missing a numeric ``ts`` (a scrubbed or partial write) are
    excluded rather than compared, so a bad line can never widen the slice.
    """
    return window.contains(record.get("ts"))


def splice_capture(capture_path: Path, out_path: Path, window: CaptureWindow) -> int:
    """Slice ``capture/capture.jsonl`` to the run window and write the fixture.

    Reads the working capture the backend appended to during the run, keeps
    the records whose ``ts`` lands in ``window``, and writes them (verbatim
    JSONL lines, original order) to ``out_path`` — the committed
    ``capture.real-client.jsonl`` fixture. ``capture_path`` is never written.

    A missing source — or a window that matches no records (a degenerate or
    failed run) — leaves ``out_path`` untouched, so a good fixture is never
    clobbered to empty. Returns the number of records written.
    """
    if not capture_path.exists():
        return 0
    kept: list[str] = []
    for line in capture_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a partial line from a concurrent append — skip it
        if capture_in_window(record, window):
            kept.append(line)
    if not kept:
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return len(kept)


def reset_capture_dir(capture_dir: Path) -> None:
    """Empty the working capture dir so a run starts from a clean slate.

    A prior run's ``capture.jsonl`` would otherwise bleed into this run's
    window slice (the record timestamps would be older than ``start_ts``,
    but the file must start empty to guarantee that). Ensures the dir exists
    because the backend's capture middleware appends without creating it.
    """
    if capture_dir.exists():
        for child in capture_dir.iterdir():
            if child.is_file():
                child.unlink()
    else:
        capture_dir.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# backend cold-start + readiness
# --------------------------------------------------------------------------


def cold_start(log_path: Path, port: int) -> subprocess.Popen[bytes]:
    """Start ``stdbuf -oL uvicorn cs_uk_api.main:app`` with log capture.

    ``stdbuf -oL`` line-buffers stdout so the runner can tail in real
    time; both stdout and stderr go to ``backend.log`` because the
    request-log middleware writes through Python logging (stderr).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The handle outlives this function — Popen keeps writing to it for the
    # subprocess's lifetime, so it cannot be scoped to a `with` block. If
    # Popen itself raises, close it before propagating.
    log_fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    uvicorn = BACKEND_DIR / ".venv" / "bin" / "uvicorn"
    if not uvicorn.exists():
        uvicorn = Path("uvicorn")
    cmd = [
        "stdbuf",
        "-oL",
        str(uvicorn),
        "cs_uk_api.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
    ]
    env = dict(os.environ)
    env["CS_UK_JF_CAPTURE_DIR"] = str(CAPTURE_DIR)
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(BACKEND_DIR),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except BaseException:
        log_fh.close()
        raise


def wait_for_backend(host: str, port: int, timeout: float = READY_TIMEOUT_S) -> None:
    """Poll an unauthenticated endpoint until uvicorn answers."""
    url = f"http://{host}:{port}/System/Info/Public"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise RuntimeError("backend did not become ready")


def _stop_backend(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the backend subprocess, escalating to kill on a hung exit."""
    proc.terminate()
    try:
        proc.wait(timeout=SHUTDOWN_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()


# --------------------------------------------------------------------------
# the runner core (sequencing + detection)
# --------------------------------------------------------------------------


class Runner:
    """Drives a step list against a tailed backend.log + an adb device."""

    def __init__(
        self,
        steps: list[Step],
        tap_coords: dict[str, tuple[int, int]],
        tailer: LogTailer,
        adb: Adb,
        *,
        host: str,
        port: int,
        timeout_s: float,
        issue_fn: Callable[[Step, dict[str, str]], None] | None = None,
        probe_fn: Callable[[dict[str, str]], str | None] | None = None,
        request_timeout: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self._steps = steps
        self._taps = tap_coords
        self._tailer = tailer
        self._adb = adb
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._request_timeout = request_timeout
        self._issue_fn = issue_fn or self._issue_default
        self._probe_fn = probe_fn or self._probe_default
        # Probe once — a device doesn't vanish mid-run, and per-step checks
        # would spawn an `adb devices` subprocess for every skipped step.
        self._adb_available = adb.available()
        self._ctx: dict[str, str] = {}

    # -- public -----------------------------------------------------------

    def run(self) -> list[StepResult]:
        results: list[StepResult] = []
        failed_views: set[str] = set()
        for index, step in enumerate(self._steps):
            if self._adb_available:
                self._adb.marker(f"STEP_{index + 1}_{step.name}")
            if step.phase in ("handshake", "warmup"):
                result = self._run_handshake(step)
                results.append(result)
                if not result.ok and step.phase == "handshake":
                    # #144: stop, exit non-zero
                    break
                # A failed warmup (B1) is a precondition, not a test: a
                # missed prime just means the app may hit a slow first
                # open, which the open step reports on its own. Keep going.
                continue
            if step.phase == "nav":
                result = self._run_nav(step)
                results.append(result)
                if not result.ok:
                    break  # device vanished — stop, don't fake the rest
                continue
            if step.view and step.view in failed_views:
                results.append(
                    StepResult(
                        step.name,
                        step.phase,
                        step.view,
                        ok=False,
                        skipped=True,
                        note="earlier step in this view failed",
                    )
                )
                continue
            result = self._run_view_step(step)
            results.append(result)
            if not result.ok and step.view:
                failed_views.add(step.view)
        return results

    # -- step handlers ----------------------------------------------------

    def _run_handshake(self, step: Step) -> StepResult:
        scan_from = len(self._tailer.all_lines())
        try:
            self._issue_fn(step, self._ctx)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the runner
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                note=f"request failed: {exc}",
            )
        ok = self._wait_for_expects(step.expects, scan_from)
        return StepResult(
            step.name,
            step.phase,
            step.view,
            ok=ok,
            timed_out=not ok,
            note=("" if ok else f"timeout waiting for {step.expects[0].request}"),
            window_lines=tuple(self._tailer.all_lines()[scan_from:]),
        )

    def _run_nav(self, step: Step) -> StepResult:
        """Press BACK ``step.nav`` times to return to the library grid.

        The real client has no visible "back to grid" button on detail/play
        screens, so the runner navigates with BACK keyevents (device-driving
        B6). A nav step issues no HTTP request and passes trivially; a device
        that vanished mid-run fails here so the run stops cleanly.
        """
        if not self._adb_available:
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                skipped=True,
                note="adb device not available",
            )
        try:
            for _ in range(step.nav):
                self._adb.back()
        except (OSError, subprocess.CalledProcessError) as exc:
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                note=f"adb back failed: {exc}",
            )
        return StepResult(step.name, step.phase, step.view, ok=True)

    def _safe_tap(self, coords: tuple[int, int], note_prefix: str = "") -> str | None:
        """Run one adb tap, converting a mid-run device failure into a note.

        Returns None on success; on failure returns a note of the form
        ``"{note_prefix}: adb tap failed: {exc}"`` (bare ``"adb tap failed:
        {exc}"`` when the prefix is empty) so the caller records the failure
        and the run continues instead of crashing and losing its results
        (#153).
        """
        try:
            self._adb.tap(*coords)
        except (OSError, subprocess.CalledProcessError) as exc:
            if note_prefix:
                return f"{note_prefix}: adb tap failed: {exc}"
            return f"adb tap failed: {exc}"
        return None

    def _run_view_step(self, step: Step) -> StepResult:
        if step.branches:
            return self._run_play(step)
        if not step.tap:
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                note="step has no tap reference",
            )
        coords = self._taps.get(step.tap)
        if coords is None:
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                note=f"no calibration for '{step.tap}'; run --calibrate",
            )
        if not self._adb_available:
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                skipped=True,
                note="adb device not available",
            )
        scan_from = len(self._tailer.all_lines())
        failure = self._safe_tap(coords)
        if failure is not None:
            # device vanished mid-run — record a ❌, don't crash and lose the
            # whole run's results
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                note=failure,
            )
        ok = self._wait_for_expects(step.expects, scan_from)
        return StepResult(
            step.name,
            step.phase,
            step.view,
            ok=ok,
            timed_out=not ok,
            note=(
                ""
                if ok
                else f"timeout waiting for {', '.join(e.request for e in step.expects)}"
            ),
            window_lines=tuple(self._tailer.all_lines()[scan_from:]),
        )

    def _run_play(self, step: Step) -> StepResult:
        item_type = self._probe_fn(self._ctx)
        if item_type is None:
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                note="could not determine item Type (detail probe failed)",
            )
        branch = self._select_branch(step, item_type)
        if branch is None:
            return StepResult(
                step.name,
                step.phase,
                step.view,
                ok=False,
                note=f"no branch for Type '{item_type}'",
            )
        step_start = len(self._tailer.all_lines())
        notes: list[str] = []
        for play_tap in branch.taps:
            coords = self._taps.get(play_tap.tap)
            if coords is None:
                notes.append(f"{play_tap.tap}: no calibration")
                continue
            if not self._adb_available:
                notes.append(f"{play_tap.tap}: adb device not available")
                break
            scan_from = len(self._tailer.all_lines())
            failure = self._safe_tap(coords, play_tap.tap)
            if failure is not None:
                notes.append(failure)
                break
            if not self._wait_for_expects(play_tap.expects, scan_from):
                notes.append(f"{play_tap.tap}: timeout")
        ok = not notes
        return StepResult(
            step.name,
            step.phase,
            step.view,
            ok=ok,
            timed_out=not ok,
            note="; ".join(notes),
            window_lines=tuple(self._tailer.all_lines()[step_start:]),
        )

    def _select_branch(self, step: Step, item_type: str) -> Branch | None:
        key = "movie" if item_type == "Movie" else "series"
        for branch in step.branches:
            if branch.key == key:
                return branch
        return None

    # -- detection --------------------------------------------------------

    def _wait_for_expects(self, expects: tuple[Expect, ...], scan_from: int) -> bool:
        """True once every expect matched a new log line within the timeout."""
        if not expects:
            return True
        deadline = time.monotonic() + self._timeout_s
        matched: set[int] = set()
        while time.monotonic() < deadline:
            for line in self._tailer.all_lines()[scan_from:]:
                for index, exp in enumerate(expects):
                    if index in matched:
                        continue
                    m = re.search(exp.request, line)
                    if not m:
                        continue
                    status = status_of(line)
                    if status is None or status not in exp.status:
                        continue
                    matched.add(index)
                    if exp.capture and exp.capture in (m.groupdict() or {}):
                        self._ctx[exp.capture] = m.group(exp.capture)
            if len(matched) == len(expects):
                return True
            time.sleep(POLL_INTERVAL_S)
        return False

    # -- real (default) I/O -----------------------------------------------

    def _issue_default(self, step: Step, ctx: dict[str, str]) -> None:
        if step.use_token and not ctx.get("token"):
            raise RuntimeError("no token captured yet")
        path = issue_path(step, ctx)
        headers = {"Content-Type": "application/json"}
        if ctx.get("token"):
            headers["X-Emby-Token"] = ctx["token"]
        data = json.dumps(step.body).encode() if step.body is not None else None
        req = urllib.request.Request(
            f"http://{self._host}:{self._port}{path}",
            data=data,
            headers=headers,
            method=step.method or "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._request_timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{step.method} {path} -> {exc.code}") from exc
        if step.capture_token:
            obj = json.loads(payload)
            ctx["token"] = str(obj["AccessToken"])
            ctx["user_id"] = str(obj["User"]["Id"])

    def _probe_default(self, ctx: dict[str, str]) -> str | None:
        """Read the first card's ``Type`` via a self-issued detail request.

        Returns ``None`` when the item key/user are unknown or the probe
        fails, so the caller can fail the play step loudly instead of
        silently misrouting a Movie into the 4-tap Series branch.
        """
        gk = ctx.get("gk")
        user_id = ctx.get("user_id")
        if not gk or not user_id:
            return None
        url = f"http://{self._host}:{self._port}/Users/{user_id}/Items/{gk}"
        headers = {"X-Emby-Token": ctx.get("token", "")}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
                obj = json.loads(resp.read())
            item_type = obj.get("Type")
            if not item_type:
                return None
            return str(item_type)
        except (urllib.error.URLError, OSError, ValueError):
            return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="switchfin_test.py",
        description=(
            "ADB-driven manual test runner for the Switchfin facade: "
            "cold-start the backend, drive a real client, verify via the request log."
        ),
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="run the interactive tap-calibration walkthrough first",
    )
    parser.add_argument(
        "--skip-calibrate",
        action="store_true",
        help="reuse existing tap-coords.yaml without re-calibrating",
    )
    parser.add_argument("--steps", type=Path, default=DEFAULT_STEPS)
    parser.add_argument("--tap-coords", type=Path, default=DEFAULT_TAP_COORDS)
    parser.add_argument("--log", type=Path, default=DEFAULT_BACKEND_LOG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--timeout", type=float, default=None, help="per-step timeout (s)"
    )
    parser.add_argument(
        "--app-version", default="n/a", help="Switchfin build version for the report"
    )
    return parser.parse_args(argv)


def build_meta(args: argparse.Namespace, adb: Adb) -> ReportMeta:
    verified = adb.available()
    if verified:
        android = adb.shell("getprop ro.build.version.release")
        model = adb.shell("getprop ro.product.model")
        resolution = adb.shell("wm size")
    else:
        android = model = resolution = "n/a"
    return ReportMeta(
        date=datetime.now(timezone.utc).astimezone().date().isoformat(),
        android=android,
        build=args.app_version,
        backend_url=f"http://{args.host}:{args.port}",
        phone=model,
        resolution=resolution,
        verified=verified,
    )


def _is_calibrated(path: Path) -> bool:
    """True once a tap-coords file has a real (non-zero) coordinate.

    The shipped scaffold carries ``{x: 0, y: 0}`` for every element, so a
    zero-only file counts as uncalibrated and triggers the first run to
    walk the calibration loop.
    """
    coords = load_tap_coords(path)
    if not coords:
        return False
    return any(x != 0 or y != 0 for x, y in coords.values())


def _resolve_calibration(
    args: argparse.Namespace, adb: Adb, taps_path: Path
) -> int | None:
    """Decide calibration: force via --calibrate, else auto when placeholder.

    Returns an exit code to short-circuit, or ``None`` to continue the run.
    """
    if args.calibrate:
        calibrate(adb, taps_path)
        return None
    if adb.available():
        if not _is_calibrated(taps_path):
            if args.skip_calibrate:
                print(
                    "--skip-calibrate given but tap-coords.yaml has no real "
                    "coordinates; run --calibrate first.",
                    file=sys.stderr,
                )
                return 2
            calibrate(adb, taps_path)
        return None
    print(
        "No adb device found — running headless (handshake only; "
        "tap steps will be skipped).",
        file=sys.stderr,
    )
    return None


def _run_suite(
    args: argparse.Namespace,
    adb: Adb,
    steps: list[Step],
    tap_coords: dict[str, tuple[int, int]],
) -> int:
    tailer = LogTailer(args.log)
    # Host-epoch markers at the run's edges. The capture middleware stamps
    # records with the backend host's ``time.time()``; the runner emits the
    # START/END markers from that same host, so recording ``time.time()`` at
    # emission gives a window directly comparable to the capture records —
    # no phone-clock dependency.
    start_ts = time.time()
    if adb.available():
        adb.marker("SWITCHFIN_TEST_START")
    runner = Runner(
        steps,
        tap_coords,
        tailer,
        adb,
        host=args.host,
        port=args.port,
        timeout_s=args.timeout_s,
    )
    results = runner.run()
    end_ts = time.time()
    if adb.available():
        adb.marker("SWITCHFIN_TEST_END")
        dump = adb.logcat_dump()
    else:
        dump = []
    results = apply_logcat_filter(results, dump)

    window = CaptureWindow(start_ts, end_ts)
    kept = splice_capture(CAPTURE_JSONL, FIXTURE_REAL_CLIENT, window)
    print(
        f"capture: {kept} record(s) sliced to "
        f"{FIXTURE_REAL_CLIENT.relative_to(REPO_ROOT)}"
    )

    meta = build_meta(args, adb)
    args.report.write_text(render_report(results, meta), encoding="utf-8")
    write_snapshots(results, ARTIFACTS_DIR)
    print_summary(results)
    code = run_exit_code(results)
    print("PASS ✅" if code == 0 else "FAIL ❌")
    return code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    adb = Adb()
    calib = _resolve_calibration(args, adb, args.tap_coords)
    if calib is not None:
        return calib

    timeout_s, steps = load_steps(args.steps)
    args.timeout_s = args.timeout if args.timeout is not None else timeout_s
    tap_coords = load_tap_coords(args.tap_coords)

    # Empty the working capture dir before the backend starts so this run's
    # ``capture.jsonl`` (and therefore its slice) is never contaminated by a
    # previous run's records.
    reset_capture_dir(CAPTURE_DIR)
    proc = cold_start(args.log, args.port)
    try:
        wait_for_backend(args.host, args.port)
        return _run_suite(args, adb, steps, tap_coords)
    finally:
        _stop_backend(proc)


if __name__ == "__main__":
    sys.exit(main())
