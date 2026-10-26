"""GitHub issue filing for drift (spec #285, ticket #288).

The tracker boundary: a provider failing two consecutive sweeps files a
GitHub issue (the tracker the project already uses — the drift monitor
runs on the host via `gh`, which is also how this repo's own tickets
are managed); repeated failures reuse the open issue, deduped by a title
pattern, never duplicates; recovery comments and closes it.

One issue per provider AND FAULT, because the two axes the monitor judges
have independent lifecycles: a listing that recovers must not close a
provider's playback issue (its streams still do not play), and streams
playing again must not close a listing issue. The fault is therefore part
of the tracker key — the monitor says WHICH fault, and the title pattern
this module owns keeps dedupe exact.

The boundary is a tiny protocol so tests inject a recorder instead of
spawning `gh`. The real implementation shells out to `gh` (the
operator's own auth), matching the repo's existing issue workflow.
"""

from __future__ import annotations

import json
import subprocess
from typing import Literal, Protocol

#: The two faults a provider can carry an issue for: its listing/probe
#: sweep, or the playback attestation (streams served, never played).
DriftFault = Literal["sweep", "playback"]

#: Title per fault. The provider id and the fault both live in the title so
#: dedupe (``gh issue list --search``) finds the open issue for exactly this
#: provider and fault and never another's.
ISSUE_TITLE = "Drift: provider {provider_id} failing consecutive sweeps"
PLAYBACK_ISSUE_TITLE = (
    "Drift: provider {provider_id} serves streams the client never plays"
)

_TITLES: dict[DriftFault, str] = {
    "sweep": ISSUE_TITLE,
    "playback": PLAYBACK_ISSUE_TITLE,
}


def issue_title(provider_id: str, fault: DriftFault = "sweep") -> str:
    """The tracker title for one provider's issue on one axis."""
    return _TITLES[fault].format(provider_id=provider_id)


class IssueGateway(Protocol):
    def find_open(
        self, provider_id: str, fault: DriftFault = "sweep"
    ) -> str | None: ...
    def open(
        self, provider_id: str, body: str, fault: DriftFault = "sweep"
    ) -> str: ...
    def comment(self, provider_id: str, issue_number: str, body: str) -> None: ...
    def close(self, provider_id: str, issue_number: str, comment: str) -> None: ...


class GhIssueGateway:
    """Real gateway: drives the ``gh`` CLI with the operator's auth.

    Every call is a subprocess; a gh failure (CLI missing, auth broken,
    offline) surfaces as a raise that the monitor catches and logs — a
    tracker outage must not crash the sweep or lose the probe report.
    """

    def __init__(self, cwd: str | None = None) -> None:
        self._cwd = cwd

    def _run(self, args: list[str]) -> str:
        out = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=self._cwd,
            check=False,
        )
        if out.returncode != 0:
            raise RuntimeError(
                f"gh {' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}"
            )
        return out.stdout.strip()

    def find_open(self, provider_id: str, fault: DriftFault = "sweep") -> str | None:
        """The number of the open issue for this provider and fault, or None.

        GitHub's search parser returns no rows for the
        ``title:"..." in:title`` combination (observed live on the
        drift issues), while the phrase form ``in:title "..."`` finds
        them. Filter by exact title as well: a near-match must never
        be mistaken for this provider's issue — and one fault's title is
        never the other's, so a listing issue is never closed as a
        playback recovery.
        """
        title = issue_title(provider_id, fault)
        out = self._run(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                f'in:title "{title}"',
                "--json",
                "number,title",
                "--limit",
                "20",
            ]
        )
        rows = json.loads(out)
        for row in rows:
            if row.get("title") == title:
                return str(row["number"])
        return None

    def open(self, provider_id: str, body: str, fault: DriftFault = "sweep") -> str:
        """File a drift issue for this provider and fault; returns its number."""
        out = self._run(
            [
                "issue",
                "create",
                "--title",
                issue_title(provider_id, fault),
                "--body",
                body,
            ]
        )
        return out.rsplit("/", 1)[-1]

    def comment(self, provider_id: str, issue_number: str, body: str) -> None:
        self._run(["issue", "comment", issue_number, "--body", body])

    def close(self, provider_id: str, issue_number: str, comment: str) -> None:
        self._run(["issue", "close", issue_number, "--comment", comment])
