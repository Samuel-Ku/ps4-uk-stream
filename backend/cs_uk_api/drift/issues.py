"""GitHub issue filing for drift (spec #285, ticket #288).

The tracker boundary: a provider failing two consecutive sweeps files a
GitHub issue (the tracker the project already uses — the drift monitor
runs on the host via `gh`, which is also how this repo's own tickets
are managed); repeated failures reuse the open issue (one issue per
provider — deduped by a title pattern, never duplicates); recovery
comments and closes it.

The boundary is a tiny protocol so tests inject a recorder instead of
spawning `gh`. The real implementation shells out to `gh` (the
operator's own auth), matching the repo's existing issue workflow.
"""

from __future__ import annotations

import subprocess
from typing import Protocol

#: One issue per provider: the title embeds the provider id so dedupe
#: (``gh issue list --search``) finds the open issue for exactly this
#: provider and never another's.
ISSUE_TITLE = "Drift: provider {provider_id} failing consecutive sweeps"


class IssueGateway(Protocol):
    def find_open(self, provider_id: str) -> str | None: ...
    def open(self, provider_id: str, body: str) -> str: ...
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

    def find_open(self, provider_id: str) -> str | None:
        """The number of the open drift issue for this provider, or None."""
        title = ISSUE_TITLE.format(provider_id=provider_id)
        out = self._run(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                f'title:"{title}" in:title',
                "--json",
                "number",
                "--limit",
                "1",
            ]
        )
        import json

        rows = json.loads(out)
        return str(rows[0]["number"]) if rows else None

    def open(self, provider_id: str, body: str) -> str:
        """File a drift issue; returns its number."""
        out = self._run(
            [
                "issue",
                "create",
                "--title",
                ISSUE_TITLE.format(provider_id=provider_id),
                "--body",
                body,
            ]
        )
        return out.rsplit("/", 1)[-1]

    def comment(self, provider_id: str, issue_number: str, body: str) -> None:
        self._run(["issue", "comment", issue_number, "--body", body])

    def close(self, provider_id: str, issue_number: str, comment: str) -> None:
        self._run(["issue", "close", issue_number, "--comment", comment])
