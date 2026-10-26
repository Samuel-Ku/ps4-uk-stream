"""GhIssueGateway tracker-boundary tests (drift recovery regression).

Live failure (2026-10-26): GitHub's issue search returned no rows for
``title:"..." in:title``, so ``find_open`` returned None and the
nightly recovery path never closed the issue. The gateway uses the
working phrase form and filters by exact title, so a near-match cannot
be mistaken for this provider's issue.
"""

from __future__ import annotations

import json

from cs_uk_api.drift.issues import ISSUE_TITLE, GhIssueGateway


def test_find_open_searches_by_phrase_and_requires_exact_title(monkeypatch) -> None:
    title = ISSUE_TITLE.format(provider_id="anitubeinua")
    calls: list[list[str]] = []

    def fake_run(_self: GhIssueGateway, args: list[str]) -> str:
        calls.append(args)
        return json.dumps(
            [
                {"number": 999, "title": f"{title} (old)"},
                {"number": 382, "title": title},
            ]
        )

    monkeypatch.setattr(GhIssueGateway, "_run", fake_run)

    assert GhIssueGateway().find_open("anitubeinua") == "382"
    search = calls[0][calls[0].index("--search") + 1]
    assert search == f'in:title "{title}"'
