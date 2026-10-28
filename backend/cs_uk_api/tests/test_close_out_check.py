"""Close-out checker tests: docs-only mode and agent-doc red paths.

``scripts/close_out_check.py`` is outside the Python package, so each test
builds a miniature repo, copies the real script into it, and runs the CLI.
That keeps the production docs untouched while pinning the behaviour CI
relies on:

- ``--docs-only`` never invokes ``gh`` and still verifies SHA reachability;
- the doc set includes ``docs/agents/*.md``, where the close-out checklist
  itself lives;
- a closed-issue state claim in an agent doc fails the full run, while a
  placeholder example does not;
- a SHA that resolves but is not on ``origin/master`` is a finding;
- a failing ``gh`` in the full run is an environment failure (exit 2), not
  a finding;
- the full run scans open ticket bodies and comments, not just docs.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_CHECKER = Path(__file__).resolve().parents[3] / "scripts" / "close_out_check.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def _run(
    repo: Path, *args: str, path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if path is not None:
        env["PATH"] = f"{path}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "close_out_check.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _open_issue(
    number: int, body: str, comments: tuple[str, ...] = ()
) -> dict[str, object]:
    """One ``gh issue list`` entry as the checker consumes it."""
    return {
        "number": number,
        "body": body,
        "comments": [{"body": comment} for comment in comments],
    }


def _stub_gh(
    directory: Path,
    *,
    closed: tuple[int, ...] = (),
    open_issues: list[dict[str, object]] | None = None,
    marker: Path | None = None,
    fail_with: str | None = None,
) -> Path:
    """A ``gh`` that serves the two issue-list calls, records, or fails."""
    directory.mkdir()
    gh = directory / "gh"
    if fail_with is not None:
        body = (
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' {shlex.quote(fail_with)} >&2\n"
            "exit 1\n"
        )
    elif marker is not None:
        body = (
            "#!/usr/bin/env bash\n"
            f"touch {shlex.quote(str(marker))}\n"
            "exit 99\n"
        )
    else:
        open_json = json.dumps(open_issues or [])
        closed_json = json.dumps([{"number": number} for number in closed])
        body = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            'case "$*" in\n'
            f'  *"--state open"*) echo {shlex.quote(open_json)} ;;\n'
            f'  *"--state closed"*) echo {shlex.quote(closed_json)} ;;\n'
            "  *) exit 2 ;;\n"
            "esac\n"
        )
    gh.write_text(body)
    gh.chmod(0o755)
    return directory


@pytest.fixture()
def checker_repo(tmp_path: Path) -> Path:
    """A git repo the checker can treat as its own checkout."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(_CHECKER, repo / "scripts" / "close_out_check.py")

    guard = repo / "backend" / "cs_uk_api" / "scripts"
    guard.mkdir(parents=True)
    for package in (repo / "backend", repo / "backend" / "cs_uk_api", guard):
        (package / "__init__.py").write_text("")
    (guard / "doc_drift_guard.py").write_text(
        'LIVING_DOCS = ("README.md",)\nALLOWLIST: dict[str, str] = {}\n'
    )

    (repo / "README.md").write_text("# Readme\n")
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "docs" / "agents" / "domain.md").write_text("# Agent docs\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/master", head)
    return repo


def test_docs_only_needs_no_gh_and_scans_agent_docs(
    checker_repo: Path, tmp_path: Path
) -> None:
    base = _git(checker_repo, "rev-parse", "HEAD").strip()
    (checker_repo / "docs" / "agents" / "domain.md").write_text(
        f"# Agent docs\n\nLanded in {base}.\n"
    )
    marker = tmp_path / "gh-called"
    stub = _stub_gh(tmp_path / "bin", marker=marker)

    result = _run(checker_repo, "--docs-only", path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "1 living docs + 1 agent docs" in output
    assert "closed-issue state claims skipped" in output
    assert not marker.exists(), "docs-only called gh"


def test_docs_only_flags_unresolvable_sha_in_agent_doc(
    checker_repo: Path,
) -> None:
    missing = "0123456789abcdef0123456789abcdef01234567"
    (checker_repo / "docs" / "agents" / "domain.md").write_text(
        f"# Agent docs\n\nLanded in {missing}.\n"
    )

    result = _run(checker_repo, "--docs-only")
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "docs/agents/domain.md" in output
    assert missing in output
    assert "does not resolve" in output


def test_docs_only_flags_sha_not_on_origin(checker_repo: Path) -> None:
    _git(checker_repo, "checkout", "-q", "-b", "side")
    (checker_repo / "side.txt").write_text("x\n")
    _git(checker_repo, "add", "side.txt")
    _git(checker_repo, "commit", "-qm", "side")
    side = _git(checker_repo, "rev-parse", "HEAD").strip()
    (checker_repo / "README.md").write_text(f"# Readme\n\nLanded in {side}.\n")

    result = _run(checker_repo, "--docs-only")
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert side in output
    assert "not reachable from origin/master" in output


def test_docs_only_skips_closed_state_claims(checker_repo: Path) -> None:
    (checker_repo / "docs" / "agents" / "domain.md").write_text(
        "# Agent docs\n\n#443 must stay open.\n"
    )

    result = _run(checker_repo, "--docs-only")
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "closed-issue state claims skipped" in output


def test_full_mode_flags_closed_claim_in_agent_doc(
    checker_repo: Path, tmp_path: Path
) -> None:
    (checker_repo / "docs" / "agents" / "domain.md").write_text(
        "# Agent docs\n\n#443 must stay open.\n"
    )
    stub = _stub_gh(tmp_path / "bin", closed=(443,))

    result = _run(checker_repo, path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "docs/agents/domain.md" in output
    assert "#443" in output
    assert "calls closed" in output


def test_placeholder_example_is_not_a_claim(
    checker_repo: Path, tmp_path: Path
) -> None:
    (checker_repo / "docs" / "agents" / "domain.md").write_text(
        "# Agent docs\n\nThe failure class is `#<n> must stay open`.\n"
    )
    stub = _stub_gh(tmp_path / "bin", closed=(443,))

    result = _run(checker_repo, path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output


def test_missing_living_doc_is_a_finding(checker_repo: Path) -> None:
    (checker_repo / "README.md").unlink()

    result = _run(checker_repo, "--docs-only")
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "listed as a living doc but is missing" in output


def test_full_mode_gh_failure_is_environment_failure(
    checker_repo: Path, tmp_path: Path
) -> None:
    stub = _stub_gh(tmp_path / "bin", fail_with="HTTP 401: Bad credentials")

    result = _run(checker_repo, path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "close-out: gh failed" in result.stderr
    assert "HTTP 401: Bad credentials" in result.stderr
    assert "close-out: clean" not in result.stdout
    assert "FAIL" not in result.stdout


def test_full_mode_flags_closed_claim_in_open_ticket_body(
    checker_repo: Path, tmp_path: Path
) -> None:
    stub = _stub_gh(
        tmp_path / "bin",
        closed=(443,),
        open_issues=[
            _open_issue(512, "Finish the lane.\n\n#443 must stay open until the spec lands.\n")
        ],
    )

    result = _run(checker_repo, path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "1 open tickets" in output
    assert "#512 body" in output
    assert "calls closed #443" in output


def test_full_mode_scans_open_ticket_comments(
    checker_repo: Path, tmp_path: Path
) -> None:
    stub = _stub_gh(
        tmp_path / "bin",
        closed=(443,),
        open_issues=[
            _open_issue(
                512,
                "No state claims in the body.\n",
                ("Provenance only.\n", "Blocked on #443 until the spec lands.\n"),
            )
        ],
    )

    result = _run(checker_repo, path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "#512 comment 2" in output
    assert "calls closed #443" in output
    assert "#512 body" not in output


def test_full_mode_checks_shas_in_ticket_text(
    checker_repo: Path, tmp_path: Path
) -> None:
    missing = "0123456789abcdef0123456789abcdef01234567"
    stub = _stub_gh(
        tmp_path / "bin",
        open_issues=[_open_issue(512, f"Landed in {missing}.\n")],
    )

    result = _run(checker_repo, path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert f"#512 body:1 quotes {missing}, which does not resolve" in output


def test_full_mode_leaves_an_open_ticket_clean(
    checker_repo: Path, tmp_path: Path
) -> None:
    stub = _stub_gh(
        tmp_path / "bin",
        closed=(443,),
        open_issues=[_open_issue(512, "#512 must stay open for now.\n")],
    )

    result = _run(checker_repo, path=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "close-out: clean" in output


def test_missing_origin_master_is_environment_failure(checker_repo: Path) -> None:
    _git(checker_repo, "update-ref", "-d", "refs/remotes/origin/master")

    result = _run(checker_repo, "--docs-only")

    assert result.returncode == 2
    assert "origin/master is missing (fetch first)" in result.stderr


def test_shallow_clone_is_environment_failure(
    checker_repo: Path, tmp_path: Path
) -> None:
    shallow = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            checker_repo.as_uri(),
            str(shallow),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    head = _git(shallow, "rev-parse", "HEAD").strip()
    _git(shallow, "update-ref", "refs/remotes/origin/master", head)
    assert _git(shallow, "rev-parse", "--is-shallow-repository").strip() == "true"

    result = _run(shallow, "--docs-only")

    assert result.returncode == 2
    assert "shallow clone" in result.stderr
