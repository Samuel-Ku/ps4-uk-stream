"""Wiring tests for scripts/gate.sh (issue #30 fallout #37/#38/#39).

The bash script itself is not executed here (it needs uvicorn + mpv +
network); these tests pin the wiring decisions that the review fallout
tickets asked for, so a future edit cannot silently drop them:
  - #37: diagnose() (HTML capture + JS-marker scan) runs on the mpv
    FAIL path — the "not portable" verdict must be reachable.
  - #38: headers passed to mpv --http-header-fields are comma-joined
    (mpv expects a comma-separated list, not newlines).
  - #39: content() failures advance the try counter like stream
    failures instead of aborting the provider on the first hit.
"""
from __future__ import annotations

import pathlib

GATE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "gate.sh"


def test_gate_script_exists():
    assert GATE.is_file()


def test_diagnose_wired_into_fail_path_issue37():
    text = GATE.read_text(encoding="utf-8")
    # diagnose must be invoked from the FAIL path of gate_one() (after
    # the loop, on the last known mpv-failing URL) — not just defined.
    assert "diagnose()" in text
    assert "diagnose \"$provider\" \"$last_url\" \"$last_headers\"" in text
    fail_line = next(
        line for line in text.splitlines() if "GATE FAIL $provider" in line
    )
    diagnose_line = next(
        line for line in text.splitlines() if 'diagnose "$provider"' in line
    )
    assert text.index(fail_line) < text.index(diagnose_line)


def test_headers_mpv_joins_with_comma_issue38():
    text = GATE.read_text(encoding="utf-8")
    assert "paste -sd, -" in text
    assert "--http-header-fields=" in text


def test_content_failure_advances_loop_issue39():
    text = GATE.read_text(encoding="utf-8")
    # content() and stream() failures must both do:
    #   tries=$((tries + 1)) ; continue
    # A bare `return 1` in the content branch would abort the provider.
    assert "content ($cid) — trying next result" in text
    assert "stream ($cid) — trying next result" in text
    assert text.count("tries=$((tries + 1))") >= 2


def test_gate_pipeline_steps_are_present():
    text = GATE.read_text(encoding="utf-8")
    for step in (
        "api/search?q=",
        "api/content/",
        "api/stream/",
        "mpv --no-config --no-video --frames=1",
        "playability_profile",
    ):
        assert step in text


def test_gate_parses_merged_groups_contract_issue71():
    """Regression: /api/search returns merged ``groups`` (issue #71),
    not the pre-merge ``results`` array. gate.sh must parse
    ``['groups'][N]['sources'][0]['id']`` — the first per-provider
    source id of each group, which /api/content and /api/stream both
    accept — or every provider gate dies on the first JSON parse."""
    text = GATE.read_text(encoding="utf-8")
    assert "['groups']" in text
    assert "['groups'][$tries]['sources'][0]['id']" in text
    assert "['results']" not in text
