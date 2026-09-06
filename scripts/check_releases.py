#!/usr/bin/env python3
"""Fail when a pushed ``vX.Y.Z`` tag has no GitHub release.

Release-check drift guard (the v1.1.0 lesson): this repo tags first and
publishes the release right after, but v1.1.0 sat un-released for a day
and had to be backfilled retroactively. Wired as
``.github/workflows/release-check.yml`` — on every ``v*`` tag push
(with a grace retry for the tag-then-release window) plus a weekly
sweep that catches a backfill forgotten after tag day. A release that
exists only as a *draft* counts as missing: that is exactly a backfill
abandoned mid-flow.

Pure core plus one injectable query; the CLI shell is the only place
that spawns ``git``/``gh``. Stdlib only — runs on the runner's python3.
Zero tags prints ``0/0`` and passes (pre-first-tag repos); the
workflow's ``fetch-depth: 0`` guarantees tags are present here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass

TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class TagRelease:
    """One ``vX.Y.Z`` tag and whether GitHub has a (published) release."""

    tag: str
    released: bool
    draft: bool = False


def compare(
    tags: list[str], published: list[str], drafts: list[str]
) -> list[TagRelease]:
    """Pair every semver tag with its release presence (name membership)."""
    have = set(published)
    draft_only = set(drafts) - have
    return [
        TagRelease(tag=t, released=t in have, draft=t in draft_only)
        for t in sorted(tags)
        if TAG_RE.match(t)
    ]


def drift_report(checks: list[TagRelease]) -> list[str]:
    """Human-readable failure line per tag without a published release."""
    return [
        f"{c.tag}: release is a draft (unpublished)"
        if c.draft
        else f"{c.tag}: MISSING release"
        for c in checks
        if not c.released
    ]


def query() -> tuple[list[str], list[str], list[str]]:
    """All tag names plus published/draft release tag names, via git + gh."""
    tags = _git_tags()
    releases = _release_names()
    published = [name for name, is_draft in releases if not is_draft]
    drafts = [name for name, is_draft in releases if is_draft]
    return tags, published, drafts


def _git_tags() -> list[str]:
    out = subprocess.run(
        ["git", "tag", "--list", "v*"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split()


def _release_names() -> list[tuple[str, bool]]:
    out = subprocess.run(
        [
            "gh",
            "release",
            "list",
            "--limit",
            "1000",
            "--json",
            "tagName,isDraft",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [(r["tagName"], r["isDraft"]) for r in json.loads(out)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tags", help="comma-separated tag override (offline)")
    parser.add_argument(
        "--releases", help="comma-separated published-release override (offline)"
    )
    args = parser.parse_args(argv)
    if (args.tags is None) != (args.releases is None):
        parser.error("--tags and --releases must be given together")
    try:
        if args.tags is not None:
            tags = [t for t in args.tags.split(",") if t]
            published = [r for r in args.releases.split(",") if r]
            drafts: list[str] = []
        else:
            tags, published, drafts = query()
    except (subprocess.CalledProcessError, OSError) as exc:
        # OSError: missing git/gh binary on a fresh runner.
        print(f"release-check: query failed: {exc}", file=sys.stderr)
        return 2

    vtags = [t for t in tags if TAG_RE.match(t)]
    failures = drift_report(compare(tags, published, drafts))
    for line in failures:
        print(line)
    print(
        f"release-check: {len(vtags) - len(failures)}/{len(vtags)}"
        " v-tags have releases"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
