"""Release-check drift guard.

Pins for ``scripts/check_releases.py``: tag/release pairing semantics
and report wording (pure core), CLI exit codes through the
``--tags/--releases`` offline override (0 clean, 1 drift, 2 bad usage
or query failure), and the exact ``git``/``gh`` argv the shell owes —
never touching the network.

Run from the repo root: ``python3 -m pytest scripts/tests -q``
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_releases.py"
_spec = importlib.util.spec_from_file_location("check_releases", _SCRIPT)
assert _spec is not None and _spec.loader is not None
cr = importlib.util.module_from_spec(_spec)
sys.modules["check_releases"] = cr
_spec.loader.exec_module(cr)


# --- pure core: pairing + wording ------------------------------------


def test_compare_pairs_published_and_drafts_ignoring_non_semver_tags():
    checks = cr.compare(
        tags=["v1.0.0", "v1.2.0", "v2", "release-foo", "v1.2"],
        published=["v1.0.0"],
        drafts=["v1.2.0"],
    )
    assert checks == [
        cr.TagRelease(tag="v1.0.0", released=True, draft=False),
        cr.TagRelease(tag="v1.2.0", released=False, draft=True),
    ]


def test_compare_treats_draft_also_published_as_released():
    # A draft row whose name also has a published release is noise;
    # drafts only count against a tag when nothing is published.
    (check,) = cr.compare(["v1.0.0"], ["v1.0.0"], ["v1.0.0"])
    assert check.released and not check.draft


def test_drift_report_wording():
    checks = cr.compare(
        ["v1.0.0", "v1.1.0", "v1.2.0"],
        published=["v1.2.0"],
        drafts=["v1.0.0"],
    )
    assert cr.drift_report(checks) == [
        "v1.0.0: release is a draft (unpublished)",
        "v1.1.0: MISSING release",
    ]


def test_drift_report_clean_is_empty():
    assert cr.drift_report(cr.compare(["v1.0.0"], ["v1.0.0"], [])) == []


# --- CLI: offline overrides, real exit codes -------------------------


def test_cli_clean_exits_zero():
    assert cr.main(["--tags", "v1.0.0,v1.2.0", "--releases", "v1.0.0,v1.2.0"]) == 0


def test_cli_missing_release_exits_one_with_counts(capsys):
    rc = cr.main(["--tags", "v1.1.0,v1.2.0", "--releases", "v1.2.0"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "v1.1.0: MISSING release" in out
    assert "1/2 v-tags have releases" in out


def test_cli_zero_tags_passes(capsys):
    assert cr.main(["--tags", "", "--releases", ""]) == 0
    assert "0/0 v-tags have releases" in capsys.readouterr().out


def test_cli_overrides_must_come_in_pairs():
    with pytest.raises(SystemExit) as exc:
        cr.main(["--tags", "v1.0.0"])
    assert exc.value.code == 2


def test_cli_live_draft_release_exits_one(capsys):
    with patch.object(
        cr, "query", return_value=(["v1.1.0"], [], ["v1.1.0"])
    ):
        rc = cr.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "v1.1.0: release is a draft (unpublished)" in out


def test_cli_query_failure_exits_two(capsys):
    err = subprocess.CalledProcessError(128, "git tag")
    with patch.object(cr, "query", side_effect=err):
        assert cr.main([]) == 2
    assert "query failed" in capsys.readouterr().err


def test_cli_missing_gh_binary_exits_two(capsys):
    with patch.object(cr, "query", side_effect=FileNotFoundError("gh")):
        assert cr.main([]) == 2
    assert "query failed" in capsys.readouterr().err


# --- the shell contract: exact git/gh argv ---------------------------


def test_git_tags_argv_and_split():
    fake = subprocess.CompletedProcess([], 0, stdout="v1.0.0\nv1.2.0\n")
    with patch.object(cr.subprocess, "run", return_value=fake) as run:
        assert cr._git_tags() == ["v1.0.0", "v1.2.0"]
    assert run.call_args.args[0] == ["git", "tag", "--list", "v*"]


def test_release_names_argv_and_json_contract():
    payload = json.dumps(
        [
            {"tagName": "v1.2.0", "isDraft": False},
            {"tagName": "v1.1.0", "isDraft": True},
        ]
    )
    fake = subprocess.CompletedProcess([], 0, stdout=payload)
    with patch.object(cr.subprocess, "run", return_value=fake) as run:
        assert cr._release_names() == [("v1.2.0", False), ("v1.1.0", True)]
    argv = run.call_args.args[0]
    assert argv[:3] == ["gh", "release", "list"]
    # Default gh listing caps at 30 — the explicit high limit matters.
    assert "--limit" in argv and "1000" in argv
    assert "--json" in argv and "tagName,isDraft" in argv
