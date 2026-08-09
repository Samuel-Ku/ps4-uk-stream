"""Report generation + logcat error filter (ticket #146).

Pure functions over the run's ``StepResult`` list: render the Markdown
report, apply the per-step-window logcat error filter, write snapshot
files, and summarize to stdout.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.switchfin_model import (
    LOGCAT_ERROR_PATTERNS,
    VIEW_LABELS,
    ReportMeta,
    StepResult,
)

_MARKER_RE = re.compile(r"SWITCHFIN_TEST: STEP_(\d+)_")


def _marker_positions(dump: list[str]) -> dict[int, int]:
    """Map 1-based step number -> logcat line index of its ``STEP_<n>`` marker.

    A dict (not a positional list) so a marker dropped from the ring buffer
    does not shift every later step's window onto its neighbour's lines.
    """
    positions: dict[int, int] = {}
    for i, line in enumerate(dump):
        m = _MARKER_RE.search(line)
        if m:
            positions[int(m.group(1))] = i
    return positions


def apply_logcat_filter(results: list[StepResult], dump: list[str]) -> list[StepResult]:
    """Flip a step to ❌ when an error pattern lands in its logcat window.

    Window = logcat lines between the step's ``STEP_<n>_<desc>`` marker and
    the next marker. A step whose marker is missing gets no window (its
    verdict is left untouched rather than attributed the whole dump's lines).
    Skipped steps stay skipped.
    """
    positions = _marker_positions(dump)
    updated: list[StepResult] = []
    for index, result in enumerate(results):
        marker = positions.get(index + 1)
        if marker is None:
            window: tuple[str, ...] = ()
        else:
            next_marker = min(
                (p for p in positions.values() if p > marker), default=len(dump)
            )
            window = tuple(dump[marker + 1 : next_marker])
        hits = tuple(
            line
            for line in window
            if any(p.lower() in line.lower() for p in LOGCAT_ERROR_PATTERNS)
        )
        ok = result.ok
        note = result.note
        if hits and not result.skipped:
            ok = False
            note = (
                result.note
                + ("" if result.note else "; ")
                + "logcat: "
                + ", ".join(hits[:3])
            )
        updated.append(
            StepResult(
                result.name,
                result.phase,
                result.view,
                ok,
                skipped=result.skipped,
                timed_out=result.timed_out,
                note=note,
                window_lines=result.window_lines,
                logcat_hits=hits,
                logcat_window=window,
            )
        )
    return updated


def _mark(result: StepResult, skipped: str = "❌ skipped") -> str:
    """✅/skipped/❌ glyph. The report uses ``❌ skipped``, stdout ``⏭ skipped``."""
    if result.ok:
        return "✅"
    if result.skipped:
        return skipped
    return "❌"


def _render_header(results: list[StepResult], meta: ReportMeta) -> list[str]:
    verification = (
        "✅ verified on device" if meta.verified else "⚠️ unverified — no device available"
    )
    lines = [
        "# Switchfin manual test report",
        "",
        f"- Date: {meta.date}",
        f"- Android version: {meta.android}",
        f"- Switchfin build: {meta.build}",
        f"- Backend URL: {meta.backend_url}",
        f"- Phone: {meta.phone} · {meta.resolution}",
        f"- Verification: {verification}",
        "",
        "## Handshake",
        "",
    ]
    handshake = [r for r in results if r.phase == "handshake"]
    if handshake:
        lines += [f"- {r.name}: {_mark(r)}" for r in handshake]
        lines.append("")
    else:
        lines += ["- handshake aborted — no steps ran after the failure", ""]
    return lines


def _render_view_sweep(results: list[StepResult]) -> list[str]:
    lines = [
        "## View sweep",
        "",
        "| View | Open | Detail | Play |",
        "|---|---|---|---|",
    ]
    for view, label in VIEW_LABELS.items():
        cells = []
        for phase in ("open", "detail", "play"):
            match = [r for r in results if r.view == view and r.phase == phase]
            cells.append(_mark(match[0]) if match else "–")
        lines.append(f"| {label} | {' | '.join(cells)} |")
    lines.append("")
    return lines


def _render_notes(failed: list[StepResult]) -> list[str]:
    lines = ["## Notes", ""]
    if failed:
        for r in failed:
            lines.append(f"- `{r.name}` ❌ — {r.note or 'failed'}")
            for hit in r.logcat_hits[:5]:
                lines.append(f"  - `{hit.strip()}`")
            if r.logcat_window:
                lines.append("  ```")
                lines += [f"  {ln}" for ln in r.logcat_window[:12]]
                lines.append("  ```")
            lines.append("")
    else:
        lines += ["- None — no failed steps.", ""]
    return lines


def _render_verdict(results: list[StepResult]) -> list[str]:
    hard_failures = [r for r in results if not r.ok and not r.skipped]
    skipped = sum(1 for r in results if r.skipped)
    passed = len(results) - len(hard_failures) - skipped
    verdict = "PASS ✅" if not hard_failures else "FAIL ❌"
    return [
        "## Verdict",
        "",
        f"**{verdict}** — {passed} passed, {skipped} skipped, {len(hard_failures)} failed",
        "",
    ]


def render_report(results: list[StepResult], meta: ReportMeta) -> str:
    """Render docs/switchfin-test-report.md from the run results."""
    failed = [r for r in results if not r.ok and not r.skipped]
    lines = (
        _render_header(results, meta)
        + _render_view_sweep(results)
        + _render_notes(failed)
        + _render_verdict(results)
    )
    return "\n".join(lines)


def write_snapshots(results: list[StepResult], artifacts_dir: Path) -> None:
    """Write per-step snapshots for ❌ steps to ``artifacts_dir``.

    Two channels, both deliberate: ``logcat-<step>.txt`` (spec-required)
    and ``backend-<step>.txt`` (kept for triage, #150). Every ❌ step
    writes a logcat snapshot; an empty window writes a one-line note
    instead of producing no file (#149), so a timed-out step is never
    invisible to offline triage. The backend window is written only when
    it has lines (it is the runner's primary detection channel; in
    headless runs it is often the only non-empty artifact).
    """
    for result in results:
        if result.ok or result.skipped:
            continue
        logcat_path = artifacts_dir / f"logcat-{result.name}.txt"
        if result.logcat_window:
            logcat_path.write_text(
                "\n".join(result.logcat_window) + "\n", encoding="utf-8"
            )
        else:
            logcat_path.write_text(
                "no logcat lines in this step window\n", encoding="utf-8"
            )
        backend_path = artifacts_dir / f"backend-{result.name}.txt"
        if result.window_lines:
            backend_path.write_text(
                "\n".join(result.window_lines) + "\n", encoding="utf-8"
            )


def print_summary(results: list[StepResult]) -> None:
    for result in results:
        suffix = f" — {result.note}" if result.note else ""
        print(f"  {_mark(result, skipped='⏭ skipped')} {result.name}{suffix}")
    for view, label in VIEW_LABELS.items():
        view_results = [r for r in results if r.view == view]
        if not view_results:
            continue
        parts = ", ".join(
            f"{r.phase} {_mark(r, skipped='⏭ skipped')}" for r in view_results
        )
        print(f"{label}: {parts}")


def run_exit_code(results: list[StepResult]) -> int:
    """0 iff no hard failures (strict — a skipped step is not a failure)."""
    return 0 if all(r.ok or r.skipped for r in results) else 1
