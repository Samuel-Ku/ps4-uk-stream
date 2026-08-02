"""Merge-core tests over live-captured fixtures (issue #76, v3 spec §4.4).

These fixtures are real provider output, frozen on 2026-08-02 (see
``fixtures/live_merge/README.md`` for provenance: query "дюна", 7 providers
queried, 5 with results). The assertions below were written against what the
capture actually produced — examined first, then encoded.

Observed capture facts encoded here:

* 25 items across 5 non-empty provider captures, all with ``year=None``
  (upstream search cards don't expose years) — the year-soft rule is NOT
  exercised by this capture; synthetic cases in ``test_merge.py`` cover it.
* Real cross-provider duplicates exist and merge into known groups, e.g.:
  - movie "Дюна": eneyida (3x) + kinovezha + klontv (2x) = 6 sources / 3 providers
  - series "Дюна": kinotron (2x) + klontv + serialno = 5 sources / 3 providers
  - movie "Дюна: Частина друга": eneyida + klontv
  - series "Дюна: Пророцтво": kinotron + klontv
  - movie "«Дюна» Ходоровського": eneyida + klontv (quote-variant titles)
"""
from __future__ import annotations

import json
from pathlib import Path

from cs_uk_api.merge import (
    effective_year,
    item_group_key,
    merge_results,
    title_aliases,
)
from cs_uk_api.models import SearchResult

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "live_merge"


def _load_items() -> list[SearchResult]:
    items: list[SearchResult] = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        items.extend(SearchResult.model_validate(d) for d in raw)
    return items


def _load_groups() -> tuple[list[SearchResult], list]:
    items = _load_items()
    return items, merge_results(items)


def _group_containing(groups: list, item_id: str):
    for g in groups:
        if any(s.id == item_id for s in g.sources):
            return g
    return None


# ---------------------------------------------------------------------------
# Capture provenance
# ---------------------------------------------------------------------------

def test_fixtures_captured_from_at_least_three_live_providers() -> None:
    """Acceptance criterion for #76: ≥3 providers, one query each."""
    files = sorted(FIXTURES_DIR.glob("*.json"))
    assert len(files) >= 3
    with_data = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw:
            with_data.append(path.stem)
    assert len(with_data) >= 3


def test_every_provider_capture_has_expected_shape() -> None:
    """Frozen snapshots: every captured item round-trips through the model
    and its provider field matches the owning file."""
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for d in raw:
            it = SearchResult.model_validate(d)
            assert it.provider == path.stem


# ---------------------------------------------------------------------------
# Merge invariants over the real data
# ---------------------------------------------------------------------------

def test_merged_group_count_is_bounded_by_item_count() -> None:
    items, groups = _load_groups()
    # Frozen snapshot: 7+7+9+1+1 = 25 items (bambooua/doramyworld empty).
    assert len(items) == 25
    assert len(groups) <= len(items)
    assert 0 < len(groups) < len(items)
    multi = [g for g in groups if len(g.sources) > 1]
    assert len(multi) == 5


def test_movie_duna_group_spans_three_providers() -> None:
    """The biggest real duplicate cluster: movie "Дюна" on eneyida (3x),
    kinovezha, klontv (2x) — 6 items / 3 providers, one merged group."""
    _, groups = _load_groups()
    g = _group_containing(groups, "kinovezha:1884-dyuna")
    assert g is not None
    assert len(g.sources) == 6
    assert {s.provider for s in g.sources} == {"eneyida", "kinovezha", "klontv"}
    assert {s.type for s in g.sources} == {"movie"}
    assert {s.title for s in g.sources} == {"Дюна"}
    # All members agree on the same own key -> group key is that key.
    assert len({item_group_key(s) for s in g.sources}) == 1
    assert g.key == item_group_key(g.sources[0])


def test_series_duna_group_spans_three_providers() -> None:
    """Series "Дюна": kinotron (2x), klontv, serialno — 5 items / 3 providers."""
    _, groups = _load_groups()
    g = _group_containing(groups, "serialno:1398-dyuna")
    assert g is not None
    assert len(g.sources) == 5
    assert {s.provider for s in g.sources} == {"kinotron", "klontv", "serialno"}
    assert {s.type for s in g.sources} == {"series"}
    assert {s.title for s in g.sources} == {"Дюна"}


def test_known_cross_provider_pairs_merge() -> None:
    _, groups = _load_groups()

    part2 = _group_containing(groups, "eneyida:films/9366-duna-chastyna-druga")
    assert part2 is not None and len(part2.sources) == 2
    assert {s.provider for s in part2.sources} == {"eneyida", "klontv"}
    assert {s.title for s in part2.sources} == {"Дюна: Частина друга"}

    prophecy = _group_containing(groups, "kinotron:7300-djuna-proroctvo")
    assert prophecy is not None and len(prophecy.sources) == 2
    assert {s.provider for s in prophecy.sources} == {"kinotron", "klontv"}
    assert {s.title for s in prophecy.sources} == {"Дюна: Пророцтво"}

    # Quote-variant titles unify: «Дюна» (eneyida) vs "Дюна" (klontv).
    hodor = _group_containing(groups, "eneyida:films/3752-dyuna-hodorovskogo")
    assert hodor is not None and len(hodor.sources) == 2
    assert {s.provider for s in hodor.sources} == {"eneyida", "klontv"}
    assert {s.title for s in hodor.sources} == {"«Дюна» Ходоровського", '"Дюна" Ходоровського'}


def test_every_group_sources_are_mutually_mergeable() -> None:
    """Re-encode the merge rule over the real groups: every pair within a
    group shares a normalized alias, matches on type, and satisfies the
    year-soft rule (same year or at least one unknown)."""
    _, groups = _load_groups()
    for g in groups:
        for a in g.sources:
            for b in g.sources:
                assert a.type == b.type
                ya, yb = effective_year(a.title, a.year), effective_year(b.title, b.year)
                assert ya is None or yb is None or ya == yb
                shared = set(title_aliases(a.title)) & set(title_aliases(b.title))
                assert shared, f"no shared alias between {a.title!r} and {b.title!r}"


def test_live_merge_is_deterministic() -> None:
    """Running merge twice (and once reversed) yields the same group keys."""
    items, _ = _load_groups()
    first = sorted(g.key for g in merge_results(items))
    second = sorted(g.key for g in merge_results(items))
    assert first == second
    reversed_keys = sorted(g.key for g in merge_results(list(reversed(items))))
    assert reversed_keys == first
    assert all(k.startswith("g1:") for k in first)


def test_fixture_groups_key_on_item_identity() -> None:
    """Every group key is one of its members' own stateless item keys
    (issue #69: stateless cross-provider identity)."""
    _, groups = _load_groups()
    for g in groups:
        own = {item_group_key(s) for s in g.sources}
        assert g.key in own
