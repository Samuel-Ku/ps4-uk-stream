"""GhIssueGateway tracker-boundary tests (drift recovery regression).

Live failure (2026-10-26): GitHub's issue search returned no rows for
``title:"..." in:title``, so ``find_open`` returned None and the
nightly recovery path never closed the issue. The gateway uses the
working phrase form and filters by exact title, so a near-match cannot
be mistaken for this provider's issue.
"""

from __future__ import annotations

import json

from cs_uk_api.drift.issues import (
    ISSUE_TITLE,
    PLAYBACK_ISSUE_TITLE,
    GhIssueGateway,
    issue_title,
)


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


def test_each_fault_carries_its_own_title() -> None:
    """The provider id is not the whole key any more: the two axes have
    independent lifecycles, so their issues must never dedupe onto each
    other."""
    sweep = issue_title("anitubeinua")
    playback = issue_title("anitubeinua", "playback")

    assert sweep != playback
    assert ISSUE_TITLE.format(provider_id="anitubeinua") == sweep
    assert PLAYBACK_ISSUE_TITLE.format(provider_id="anitubeinua") == playback
    assert "anitubeinua" in sweep and "anitubeinua" in playback


def test_find_open_searches_the_faults_own_title(monkeypatch) -> None:
    """A search for the playback issue must not answer with the sweep issue
    that happens to be open for the same provider."""
    sweep = issue_title("anitubeinua")
    playback = issue_title("anitubeinua", "playback")
    calls: list[list[str]] = []

    def fake_run(_self: GhIssueGateway, args: list[str]) -> str:
        calls.append(args)
        return json.dumps(
            [
                {"number": 382, "title": sweep},
                {"number": 431, "title": playback},
            ]
        )

    monkeypatch.setattr(GhIssueGateway, "_run", fake_run)

    gateway = GhIssueGateway()
    assert gateway.find_open("anitubeinua", "playback") == "431"
    assert gateway.find_open("anitubeinua") == "382"
    searches = [args[args.index("--search") + 1] for args in calls]
    assert searches == [f'in:title "{playback}"', f'in:title "{sweep}"']
