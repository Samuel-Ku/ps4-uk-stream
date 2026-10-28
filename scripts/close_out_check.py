#!/usr/bin/env python3
"""Close-out checker — read-only.

Checks the two mechanical parts of the close-out checklist in
``docs/agents/issue-tracker.md``:

1. every commit SHA quoted on an open ticket or in a living doc resolves
   on origin (``git merge-base --is-ancestor <sha> origin/master``);
2. no open ticket or living doc still calls a closed issue open or
   blocking. State sentences are scanned per sentence, so a line that
   cites a closed ticket as provenance ("#443 is the backend spec")
   next to an open ticket's "stays open" claim is not a finding.

The checklist's third part (one home per fact) is a judgement call and is
not checked.

Read-only: GitHub reads via ``gh issue list``, git reads only, no fetch.

Usage:
    python3 scripts/close_out_check.py

Exit: 0 clean, 1 findings, 2 environment failure (gh, git, shallow clone).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
NUM_RE = re.compile(r"#(\d+)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
URL_RE = re.compile(r"https?://\S+")
FENCE_RE = re.compile(r"^\s*```")
#: Characters that make a hex run part of a longer identifier — catalog ids
#: (``cikavaideya:g1:16cd…``), magnets (``btih:08ada…``), URL paths.
ID_GLUE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-/"
)

#: A sentence matches one of these when it claims an issue's state. The
#: number(s) are extracted by :func:`state_numbers`; matching the phrase
#: alone is not enough.
LEAVES_OPEN = re.compile(r"\bleaves?\b(.*?)\bopen\b", re.IGNORECASE | re.DOTALL)
DIRECT = re.compile(
    r"\b(?:blocked\s+(?:on|by)|waiting\s+on|depends?\s+on|until)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)
SUBJECT_OPEN = re.compile(
    r"\b(?:must|stays?|remains?|still)\s+(?:stay\s+)?open\b", re.IGNORECASE
)


def state_numbers(sentence: str) -> set[str]:
    """Issue numbers a sentence claims are open/blocking."""
    numbers: set[str] = set()
    for match in LEAVES_OPEN.finditer(sentence):
        numbers.update(NUM_RE.findall(match.group(1)))
    for match in DIRECT.finditer(sentence):
        numbers.add(match.group(1))
    for match in SUBJECT_OPEN.finditer(sentence):
        before = NUM_RE.findall(sentence[: match.start()])
        if before:
            numbers.add(before[-1])
    return numbers


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, check=False, text=True
    )


def gh_json(*args: str) -> list[dict[str, object]]:
    result = subprocess.run(
        ["gh", *args], capture_output=True, check=False, cwd=REPO, text=True
    )
    if result.returncode != 0:
        print(
            f"close-out: gh failed ({' '.join(args)}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return json.loads(result.stdout)


def sha_problem(sha: str, cache: dict[str, str | None]) -> str | None:
    """None when the SHA resolves on origin, else the reason it does not."""
    if sha in cache:
        return cache[sha]
    problem: str | None
    resolved = git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if resolved.returncode != 0:
        problem = "does not resolve"
    elif git("merge-base", "--is-ancestor", sha, "origin/master").returncode != 0:
        problem = "is not reachable from origin/master"
    else:
        problem = None
    cache[sha] = problem
    return problem


def shas_on_line(line: str, allowlist: dict[str, str]) -> set[str]:
    """Commit-like tokens on a line, excluding documented non-commits."""
    tokens: set[str] = set()
    url_spans = [match.span() for match in URL_RE.finditer(line)]
    for match in SHA_RE.finditer(line):
        token = match.group(0)
        if token.isdigit() or token in allowlist:
            continue
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        before = line[match.start() - 1] if match.start() else ""
        after = line[match.end()] if match.end() < len(line) else ""
        if before in ID_GLUE or after in ID_GLUE:
            continue
        if line[match.end():match.end() + 1] == "…":
            continue  # truncated id, not a citation
        tokens.add(token)
    return tokens


def scan(
    label: str,
    text: str,
    closed: set[str],
    allowlist: dict[str, str],
    sha_cache: dict[str, str | None],
    findings: list[str],
    shas: set[str],
) -> None:
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for sentence in SENTENCE_END.split(line):
            stale = sorted(state_numbers(sentence) & closed, key=int)
            if stale:
                refs = ", ".join(f"#{number}" for number in stale)
                snippet = " ".join(sentence.split())[:140]
                findings.append(
                    f"{label}:{lineno} calls closed {refs} open/blocking — {snippet}"
                )
        for sha in sorted(shas_on_line(line, allowlist)):
            shas.add(sha)
            problem = sha_problem(sha, sha_cache)
            if problem:
                findings.append(f"{label}:{lineno} quotes {sha}, which {problem}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only close-out check for open tickets and living docs."
    )
    parser.parse_args()

    if git("rev-parse", "--verify", "--quiet", "origin/master").returncode != 0:
        print("close-out: origin/master is missing (fetch first)", file=sys.stderr)
        return 2
    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        print(
            "close-out: shallow clone — quoted SHAs cannot be verified; "
            "fetch full history",
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(REPO / "backend"))
    try:
        from cs_uk_api.scripts.doc_drift_guard import ALLOWLIST, LIVING_DOCS
    except ImportError as exc:  # pragma: no cover - environment failure
        print(f"close-out: cannot import the living-doc list: {exc}", file=sys.stderr)
        return 2

    open_issues = gh_json(
        "issue", "list", "--state", "open", "--limit", "200",
        "--json", "number,body,comments",
    )
    closed = {
        str(issue["number"])
        for issue in gh_json(
            "issue", "list", "--state", "closed", "--limit", "1000",
            "--json", "number",
        )
    }

    findings: list[str] = []
    shas: set[str] = set()
    sha_cache: dict[str, str | None] = {}

    for issue in open_issues:
        number = issue["number"]
        scan(f"#{number} body", str(issue.get("body") or ""), closed, ALLOWLIST,
             sha_cache, findings, shas)
        for index, comment in enumerate(issue.get("comments") or [], 1):
            scan(f"#{number} comment {index}", str(comment.get("body") or ""),
                 closed, ALLOWLIST, sha_cache, findings, shas)

    for doc in LIVING_DOCS:
        path = REPO / doc
        if not path.is_file():
            findings.append(f"{doc} is listed as a living doc but is missing")
            continue
        scan(doc, path.read_text(encoding="utf-8"), closed, ALLOWLIST, sha_cache,
             findings, shas)

    print(
        f"close-out: {len(open_issues)} open tickets, {len(LIVING_DOCS)} living docs, "
        f"{len(shas)} distinct SHA-like tokens"
    )
    for finding in findings:
        print(f"  FAIL  {finding}")
    if findings:
        print(f"close-out: {len(findings)} finding(s)")
        return 1
    print("close-out: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
