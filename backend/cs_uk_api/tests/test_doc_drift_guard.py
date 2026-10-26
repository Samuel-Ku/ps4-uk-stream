"""Doc-drift guard tests: backticked refs in living docs must resolve.

The 2026-09-19 sweep found five real drifts — dead symbols named as
load-bearing fact in the living docs — because nothing failed when code
moved and docs did not. ``cs_uk_api/scripts/doc_drift_guard.py`` is the
accumulation guard; these tests pin it: the current tree passes, the
dead symbols the sweep caught would fail, and the resolver's
false-positive classes (from the sweep's own triage) stay resolved.

The one environment-sensitive class is pinned too: a bare commit SHA
resolves against the object database, so a SHALLOW checkout cannot
verify the commits ``docs/v1-deviations.md`` cites. The gate checks out
full history for that reason, and a shallow run says so instead of
excusing the spans.
"""

from __future__ import annotations

from pathlib import Path

from cs_uk_api.scripts.doc_drift_guard import (
    ALLOWLIST,
    classify_and_resolve,
    collect_drift,
    is_shallow,
    shallow_sha_hint,
)

_REPO = Path(__file__).resolve().parents[3]


def test_current_tree_has_no_doc_drift() -> None:
    """The guard passes on the tree as committed — the gate itself."""
    drift = collect_drift(_REPO)
    assert drift == [], (
        shallow_sha_hint(drift, is_shallow_repo=is_shallow(_REPO))
        or "living docs name symbols the tracked codebase no longer defines"
    )


def test_a_shallow_checkout_cannot_verify_cited_sha() -> None:
    """The environment-sensitive class, named instead of excused.

    The gate checks out full history so the cited commits ARE checkable;
    a shallow clone cannot check them at all, so the guard fails WITH the
    environment named. Silently resolving them would switch the check off
    for a typo'd SHA too — the one thing this class exists to catch.
    """
    assert not is_shallow(_REPO), (
        "this checkout is shallow — cited commit SHAs cannot be verified "
        "and the guard fails here for that reason; fetch full history"
    )
    sha_drift = [("docs/v1-deviations.md", 29, "1b55c56")]
    # A complete repository: the span is real drift, no environment note.
    assert shallow_sha_hint(sha_drift, is_shallow_repo=False) is None
    # Shallow, but nothing SHA-like unresolved: nothing to explain.
    assert shallow_sha_hint([("README.md", 3, "resolve_stream")], is_shallow_repo=True) is None
    hint = shallow_sha_hint(sha_drift, is_shallow_repo=True)
    assert hint is not None
    assert "SHALLOW" in hint
    assert "fetch-depth: 0" in hint  # what fixes it
    assert "1b55c56" in hint  # which claim could not be checked


def test_the_sweeps_dead_symbols_fail_the_guard() -> None:
    """Every symbol the 2026-09-19 sweep caught must be flagged, not just
    the two the docs still mention. Reintroducing any of them as live
    fact (outside its one sanctioned history-pointer use) must trip."""
    dead = {
        "catalog_state.CatalogStores(settings)",
        "STORES",
        "main._browse_cache",
        "DirectPlayerResponse",
        "probe_rail.py",
    }
    drift = {
        span for _, _, span in collect_drift(
            _REPO, docs=("docs/architecture.md",)
        )
    }
    assert not dead & drift, (
        "sweep-caught dead names flagged — they are allowlisted/renamed, "
        "the docs must not reintroduce them"
    )


def test_dead_symbols_resolve_when_reintroduced() -> None:
    """The discriminator: a dead name is *unresolvable* — removing it
    from the allowlist and asking the resolver directly must return
    False. If these start returning True the guard has gone blind to
    exactly the drift it exists to catch."""
    assert "main._browse_cache" in ALLOWLIST  # the one sanctioned pointer
    files = ["backend/cs_uk_api/main.py", "backend/cs_uk_api/_stores.py"]
    corpus = "def merged_search():\nbrowse_cache = TtlCache()\n"
    for dead in ("catalog_state.CatalogStores", "DirectPlayerResponse",
                 "STORES", "profile_store"):
        assert not classify_and_resolve(
            dead,
            repo=_REPO,
            corpus=corpus,
            defined=set(),
            files=files,
            md=[],
        ), dead


def test_resolver_false_positive_classes() -> None:
    """The classes the sweep's triage established must stay resolved."""
    files = [
        "backend/cs_uk_api/main.py",
        "backend/cs_uk_api/_catalog_state/_stores.py",
        "backend/cs_uk_api/tests/test_versioned_store.py",
        "backend/cs_uk_api/versioned_store.py",
        "backend/cs_uk_api/scripts/refresh_uakino.py",
        "backend/deploy/smoke.sh",
        "docs/architecture.md",
    ]
    corpus = (
        "user-state.json\ncs_uk_api/_catalog_state/_stores.py\n"
        "CS_UK_API_URL=\"${CS_UK_API_URL:-http://127.0.0.1:8003}\"\n"
        "def merged_search():\n"
        "rows = merge.group_key(\n"
    )
    md = ["docs/architecture.md"]
    ok = [
        "tests/test_versioned_store.py",      # prefix-relative path
        "versioned_store.py",                 # bare basename
        "_catalog_state/_stores.py",          # backend-root path
        "docs/architecture.md",               # doc-to-doc path
        "user-state.json",                    # runtime artifact in corpus
        "catalog_state.merged_search",        # dotted, renamed module
        "merge.group_key(a, b)",              # call syntax
        "refresh_uakino.py [--close]",        # usage form
        "https://yts.mx/api/v2",              # upstream URL
        "GET /api/search",                    # route
        "/Items/{id}",                        # route with placeholder
        "rm ~/.cache/cs-uk-api/playback.json",  # shell line
        "<provider-id>",                      # placeholder
        "a → b",                              # prose fragment
    ]
    for span in ok:
        assert classify_and_resolve(
            span, repo=_REPO, corpus=corpus, defined={"merged_search"},
            files=files, md=md,
        ), span


def test_allowlist_entries_carry_reasons() -> None:
    """Every exemption must explain itself to a future reader."""
    assert ALLOWLIST, "the allowlist must not silently empty"
    for span, reason in ALLOWLIST.items():
        assert span, "allowlist keys must be non-empty"
        assert len(reason) > 20, f"reason too thin for {span!r}: {reason!r}"
