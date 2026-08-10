"""ADB device interaction + backend.log tailing for the Switchfin runner.

Everything the runner knows about the phone comes from ``adb`` subprocesses;
everything the backend knows comes through its request log. This module wraps
both channels. No ``cs_uk_api`` import (tickets #144-#146 mandate stdlib +
``pyyaml`` only).
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from scripts.switchfin_model import CALIBRATION_ELEMENTS

#: Subprocess bounds for adb calls (devices list, shell, tap, logcat, getevent).
ADB_DEVICES_TIMEOUT_S = 5
ADB_SHELL_TIMEOUT_S = 10
GETEVENT_SAMPLE_S = 4.0

#: The Switchfin Android app under test (device-driving B17/B18).
APP_PACKAGE = "fun.dragonfly.switchfin"
APP_ACTIVITY = f"{APP_PACKAGE}/.SwitchfinActivity"


class Adb:
    """Thin wrapper over ``adb`` shell commands. Each call is a subprocess."""

    def __init__(self, binary: str = "adb") -> None:
        self.binary = binary

    def available(self) -> bool:
        try:
            out = subprocess.run(
                [self.binary, "devices"],
                capture_output=True,
                text=True,
                timeout=ADB_DEVICES_TIMEOUT_S,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        return any(line.endswith("\tdevice") for line in out.splitlines())

    #: Hold duration for a tap (ms). The Switchfin client polls touch state
    #: once per frame and drops a plain `input tap` (DOWN+UP in ~50 ms)
    #: entirely; a press held >= ~300 ms registers as a click (device-driving
    #: findings B2, 2026-08-10).
    TAP_HOLD_MS = 350

    def tap(self, x: int, y: int) -> None:
        """Tap via a held touchscreen press (see TAP_HOLD_MS).

        ``input tap`` is too fast for the SDL client's per-frame touch poll;
        ``input touchscreen swipe X Y X Y 350`` is the working tap.
        """
        subprocess.run(
            [
                self.binary,
                "shell",
                "input",
                "touchscreen",
                "swipe",
                str(x),
                str(y),
                str(x),
                str(y),
                str(self.TAP_HOLD_MS),
            ],
            check=True,
            timeout=ADB_SHELL_TIMEOUT_S,
        )

    def back(self) -> None:
        """Press the app's global BACK (exits dialog/detail/library to grid)."""
        subprocess.run(
            [self.binary, "shell", "input", "keyevent", "KEYCODE_BACK"],
            check=True,
            timeout=ADB_SHELL_TIMEOUT_S,
        )

    def screenshot_png(self) -> bytes:
        """Capture the screen as PNG bytes (device-driving, B2).

        ``adb exec-out screencap -p`` corrupts the PNG over wifi adb
        (truncated IDAT), so capture to on-device storage and pull instead.
        The file is removed from the device afterwards; the local copy is a
        tempfile that is deleted before returning.
        """
        self.shell("screencap -p /sdcard/switchfin_screenshot.png")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            subprocess.run(
                [self.binary, "pull", "/sdcard/switchfin_screenshot.png", str(tmp_path)],
                check=True,
                timeout=ADB_SHELL_TIMEOUT_S,
                capture_output=True,
            )
            return tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)
            self.shell("rm -f /sdcard/switchfin_screenshot.png")

    def marker(self, text: str) -> None:
        """Best-effort logcat marker via ``adb shell log -t``."""
        try:
            subprocess.run(
                [self.binary, "shell", "log", "-t", "SWITCHFIN_TEST", text],
                check=True,
                timeout=ADB_SHELL_TIMEOUT_S,
            )
        except (OSError, subprocess.CalledProcessError):
            pass

    def restart_app(self, settle_s: float = 2.0) -> None:
        """Force-stop and relaunch the Switchfin app (device-driving B17/B18).

        The runner otherwise drives the app's PREVIOUS session: stale grid
        ids that 404 on detail (B17) and a mid-connect state that swallows
        the first open tap (B18). Relaunching AFTER warmup means the app's
        first grid loads hit warm caches.
        """
        self.shell(f"am force-stop {APP_PACKAGE}")
        time.sleep(settle_s)
        self.shell(f"am start -n {APP_ACTIVITY}")

    def shell(self, command: str) -> str:
        try:
            return subprocess.run(
                [self.binary, "shell", command],
                capture_output=True,
                text=True,
                timeout=ADB_SHELL_TIMEOUT_S,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return "n/a"

    def logcat_dump(self) -> list[str]:
        try:
            out = subprocess.run(
                [self.binary, "logcat", "-d", "-v", "time"],
                capture_output=True,
                text=True,
                timeout=ADB_SHELL_TIMEOUT_S,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return out.splitlines()


def read_getevent(adb: Adb, duration: float = GETEVENT_SAMPLE_S) -> tuple[int, int]:
    """Sample one tap from ``adb shell getevent -l`` for ``duration`` s.

    Returns the last ABS_MT_POSITION_X/Y seen (the latest finger position).
    The loop is driven by ``select`` so the sample window is bounded even
    when the phone emits no input events — a blocking ``readline`` would
    hang the whole calibration walkthrough on a quiet device.
    """
    proc = subprocess.Popen(
        [adb.binary, "shell", "getevent", "-l"],
        stdout=subprocess.PIPE,
        text=True,
    )
    x = y = 0
    deadline = time.monotonic() + duration
    try:
        while proc.stdout is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                break  # no input within the remaining window
            line = proc.stdout.readline()
            if not line:
                break
            mx = re.search(r"ABS_MT_POSITION_X\s+([0-9a-fA-F]+)", line)
            if mx:
                x = int(mx.group(1), 16)
            my = re.search(r"ABS_MT_POSITION_Y\s+([0-9a-fA-F]+)", line)
            if my:
                y = int(my.group(1), 16)
    finally:
        proc.terminate()
        if proc.stdout is not None:
            proc.stdout.close()
        # reap so the subprocess doesn't linger as a zombie after the run;
        # escalate to kill if getevent ignores SIGTERM
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    return x, y


def calibrate(adb: Adb, taps_path: Path) -> None:
    """Interactive walkthrough: tap each labeled element, record x/y."""
    if not adb.available():
        print("No adb device found — cannot calibrate.", file=sys.stderr)
        raise SystemExit(2)
    adb.marker("CALIBRATE_START")
    coords: dict[str, dict[str, int]] = {}
    for element in CALIBRATION_ELEMENTS:
        print(f"Tap the phone at the '{element}' element…")
        x, y = read_getevent(adb)
        coords[element] = {"x": x, "y": y}
        print(f"  recorded ({x}, {y})")
    taps_path.parent.mkdir(parents=True, exist_ok=True)
    taps_path.write_text(yaml.safe_dump(coords, sort_keys=False), encoding="utf-8")
    adb.marker("CALIBRATE_END")
    print(f"Calibration saved to {taps_path}")


class LogTailer:
    """Incremental reader over the line-buffered backend.log.

    Re-opens the file on every poll and advances a monotonic byte offset,
    so no file handle is held open. ``cold_start`` truncates the log only
    before this object exists, so the offset never needs to rewind.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = os.path.getsize(path)
        self._lines: list[str] = []

    def all_lines(self) -> list[str]:
        self._read_pending()
        return self._lines

    def _read_pending(self) -> None:
        with open(self._path, "rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
            self._offset = fh.tell()
        if not data:
            return
        for line in data.decode("utf-8", "replace").splitlines():
            self._lines.append(line)
