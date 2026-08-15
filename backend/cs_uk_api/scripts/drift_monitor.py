"""Nightly upstream drift monitor (spec #285, tickets #286–#289).

Detached probe following the ``refresh_uakino.py`` precedent: it
imports the provider adapters and its own drift modules — never the
API app — runs listing probes for every plain-HTTP provider plus a
rotating deep probe (content → stream → HEAD), verdicts each against a
self-calibrating baseline, files/reuses/closes GitHub issues on two
consecutive failures, writes a machine-readable report, and exits
non-zero when any provider failed. uakino is never probed (its health
is the API's browser-session heartbeat).

Usage:
    python -m cs_uk_api.scripts.drift_monitor [--state PATH] [--report PATH]
        [--deep-every N] [--no-issues] [--day-offset N]

Exit code 0 = all providers healthy (or issue filing skipped),
1 = at least one provider failed the sweep.

Scheduling: ``backend/deploy/cs-uk-api-drift.{service,timer}`` (ticket
#288) runs this nightly; the timer's on-calendar unit is the operator's
nightly cadence.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from ..drift.monitor import DEFAULT_REPORT_PATH, DEFAULT_STATE_PATH, run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cs_uk_api.scripts.drift_monitor")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=os.environ.get("CS_UK_DRIFT_STATE", DEFAULT_STATE_PATH),
        help="path of the baseline/counter state file",
    )
    parser.add_argument(
        "--report",
        default=os.environ.get("CS_UK_DRIFT_REPORT", DEFAULT_REPORT_PATH),
        help="path of the machine-readable report file",
    )
    parser.add_argument(
        "--deep-every",
        type=int,
        default=int(os.environ.get("CS_UK_DRIFT_DEEP_EVERY", "6")),
        help="full deep-probe coverage period in days (default 6)",
    )
    parser.add_argument(
        "--day-offset",
        type=int,
        default=datetime.now().astimezone().date().toordinal(),
        help="rotation seed for the deep-probe subset (default: day ordinal)",
    )
    parser.add_argument(
        "--no-issues",
        action="store_true",
        help="run probes and verdicts but never touch the issue tracker",
    )
    args = parser.parse_args()

    from ..drift.baseline import BaselineStore
    from ..drift.issues import GhIssueGateway

    report = await run_once(
        day_offset=args.day_offset,
        deep_every_n_days=args.deep_every,
        store=BaselineStore(args.state),
        gateway=GhIssueGateway(),
        report_path=args.report,
        skip_issues=args.no_issues,
    )

    # Human summary line per provider + a failed/skipped rollup.
    for pid, run in report.providers.items():
        if run["ok"]:
            status = "OK "
        else:
            status = "FAIL"
        deep = run.get("deep")
        deep_txt = (
            f" deep={deep['ok']}"
            if deep is not None
            else " deep=-"
        )
        print(
            f"{status} {pid:<16} cards={run['listing']['count']:<4} "
            f"fails={run['consecutive_failures']}{deep_txt} "
            f"({run['reason']})"
        )
    print(f"report: {args.report}")
    if report.failed:
        print(f"FAILED providers: {', '.join(report.failed)}")
        return 1
    print("PASS all providers healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
