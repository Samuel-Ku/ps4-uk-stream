"""Tests for the cross-provider merge core (issue #52, v3 spec §4).

Seam under test: the pure functions in ``cs_uk_api.merge`` —
``normalize_title``, ``title_aliases``, ``extract_year``, ``group_key``,
``merge_results`` — plus the merge audit log. No HTTP surface.
"""
from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from cs_uk_api.merge import (
    extract_year,
    group_key,
    group_key_from,
    item_group_key,
    merge_results,
    normalize_title,
    title_aliases,
)
from cs_uk_api.models import SearchResult


def item(pid: str, title: str, media_type: str = "movie", year: int | None = None, n: str = "1") -> SearchResult:
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=cast(Any, media_type),
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------

NORMALIZE_CASES = [
    ("Дюна", "дюна"),
    ("Дюна (2021)", "дюна"),
    ("Смолфут (2018) мультфільм", "смолфут"),
    ("Залізна людина / Iron Man", "залізна людина"),
    ("Тато / Daddy", "тато"),
    ("Онґелс’", "онґелс"),  # apostrophes are deleted
    ("п'ять", "пять"),
    ("Форсаж 10 1080p WEB-DL", "форсаж 10"),
    ("Мавка. Лісова пісня", "мавка лісова пісня"),
    ("  Дюна   2  ", "дюна 2"),
    ("Хроніки Нарнії: Принц Каспіан", "хроніки нарнії принц каспіан"),
    ("Готель «Трансильванія»", "готель трансильванія"),
    ("Аватар [2009] BDRip", "аватар"),
    ("Дюна: Частина друга (фільм)", "дюна частина друга"),
    ("Кіберпростір 2 аніме", "кіберпростір 2"),  # trailing bare type word
    ("Рік 2024", "рік"),  # bare trailing 4-digit year is stripped
    ("1917", "1917"),  # ...unless stripping would leave nothing
]


@pytest.mark.parametrize("raw,expected", NORMALIZE_CASES)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


# ---------------------------------------------------------------------------
# title_aliases
# ---------------------------------------------------------------------------

def test_title_aliases_split_on_spaced_slash() -> None:
    assert set(title_aliases("Тато / Daddy")) == {"тато", "daddy"}


def test_title_aliases_single_for_plain_title() -> None:
    assert title_aliases("Дюна") == ("дюна",)


def test_title_aliases_unspaced_slash_stays_one_title() -> None:
    # "20/20" is a name, not an alias separator; '/' normalizes to a space.
    assert title_aliases("20/20") == ("20 20",)


# ---------------------------------------------------------------------------
# extract_year
# ---------------------------------------------------------------------------

YEAR_CASES = [
    ("Дюна (2021)", 2021),
    ("Смолфут [2018]", 2018),
    ("Дюна", None),
    ("Форсаж 10", None),
    ("Рік 2024", 2024),  # bare trailing year counts as the release year
    ("1917", None),  # a title that IS a year: no leading space → not extracted
]


@pytest.mark.parametrize("raw,expected", YEAR_CASES)
def test_extract_year(raw: str, expected: int | None) -> None:
    assert extract_year(raw) == expected


# ---------------------------------------------------------------------------
# group_key
# ---------------------------------------------------------------------------

def test_group_key_is_prefixed_and_stable() -> None:
    k1 = group_key("дюна", "movie", 2021)
    k2 = group_key("дюна", "movie", 2021)
    assert k1 == k2
    assert k1.startswith("g2:")
    assert len(k1) > len("g2:") + 8


def test_group_key_changes_with_any_component() -> None:
    base = group_key("дюна", "movie", 2021)
    assert group_key("дюна", "series", 2021) != base
    assert group_key("дюна", "movie", 1984) != base
    assert group_key("дюна", "movie", None) != base
    assert group_key("смолфут", "movie", 2021) != base


# ---------------------------------------------------------------------------
# merge_results — the strict + year-soft rule
# ---------------------------------------------------------------------------

def test_merge_same_title_same_year() -> None:
    groups = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Дюна", year=2021, n="2"),
    ])
    assert len(groups) == 1
    assert {s.provider for s in groups[0].sources} == {"uakino", "eneyida"}


def test_merge_extracts_year_from_title_when_field_missing() -> None:
    groups = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Дюна (2021)", n="2"),
    ])
    assert len(groups) == 1


def test_no_merge_on_conflicting_years() -> None:
    groups = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Дюна", year=1984, n="2"),
    ])
    assert len(groups) == 2


def test_merge_when_one_year_unknown() -> None:
    groups = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Дюна", n="2"),
    ])
    assert len(groups) == 1


def test_no_merge_on_type_mismatch() -> None:
    groups = merge_results([
        item("uakino", "Дюна", media_type="movie", year=2021),
        item("eneyida", "Дюна", media_type="series", year=2021, n="2"),
    ])
    assert len(groups) == 2


def test_no_merge_on_different_titles() -> None:
    groups = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Смолфут", year=2021, n="2"),
    ])
    assert len(groups) == 2


def test_merge_via_alias() -> None:
    groups = merge_results([
        item("uakino", "Смолфут", year=2018),
        item("eneyida", "Смолфут / Smallfoot", year=2018, n="2"),
    ])
    assert len(groups) == 1


def test_merge_is_transitive_via_bridge_item() -> None:
    groups = merge_results([
        item("a", "Тато", year=2020),
        item("b", "Тато / Daddy", year=2020, n="2"),
        item("c", "Daddy", n="3"),
    ])
    assert len(groups) == 1
    assert len(groups[0].sources) == 3


def test_merge_groups_have_stable_group_keys() -> None:
    forward = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Дюна (2021)", n="2"),
    ])
    reverse = merge_results([
        item("eneyida", "Дюна (2021)", n="2"),
        item("uakino", "Дюна", year=2021),
    ])
    assert len(forward) == len(reverse) == 1
    assert forward[0].key == reverse[0].key
    assert forward[0].key.startswith("g2:")


def test_item_group_key_is_pure_function_of_item_data() -> None:
    # Issue #69: the same item must produce the same key from any call,
    # regardless of which other providers appear beside it.
    it = item("uakino", "Тато / Daddy", year=2021)
    assert item_group_key(it) == group_key_from("Тато / Daddy", "movie", 2021, it.id)
    # The most canonical own alias drives the key (min over own aliases).
    assert item_group_key(it) == item_group_key(item("eneyida", "Daddy", year=2021, n="2"))


def test_merge_group_key_stable_across_provider_subsets() -> None:
    # Same semantic title across different provider subsets -> identical
    # group key. "Тато / Daddy" anchors subsets 1-3; each group's key is
    # the min of its members' own item keys (order-independent).
    anchor = item("uakino", "Тато / Daddy", year=2021)
    subsets = [
        [anchor, item("eneyida", "Daddy", year=2021, n="2")],
        [anchor, item("kinolub", "Тато", year=2021, n="3")],
        [anchor, item("eneyida", "Daddy", year=2021, n="2"), item("kinolub", "Тато", year=2021, n="3")],
    ]
    keys = []
    for subset in subsets:
        groups = merge_results(subset)
        assert len(groups) == 1
        g = groups[0]
        keys.append(g.key)
        # Real invariant: the group key is the item key of SOME member —
        # not necessarily the anchor's (which member's digest happens
        # to hash smallest is an accident of sha1 ordering).
        assert g.key in {item_group_key(it) for it in g.sources}
    assert len(set(keys)) == 1


def test_merge_group_key_min_over_members_is_order_independent() -> None:
    # A bridge item with a lexicographically smaller own alias must not
    # change the key based on input order — only on actual membership.
    items = [
        item("kinolub", "Тато", year=2021, n="3"),
        item("uakino", "Тато / Daddy", year=2021),
        item("eneyida", "Daddy", year=2021, n="2"),
    ]
    forward = merge_results(items)
    reverse = merge_results(list(reversed(items)))
    assert len(forward) == len(reverse) == 1
    assert forward[0].key == reverse[0].key
    # Real invariant: the group key is SOME member's own item key, not
    # necessarily THIS one — which member's digest hashes smallest is an
    # accident of sha1 ordering, not a property the spec depends on.
    assert forward[0].key in {item_group_key(it) for it in forward[0].sources}
    # The singleton "Тато" without the bridge keeps its own "тато" key.
    solo = merge_results([item("kinolub", "Тато", year=2021, n="3")])
    assert solo[0].key == item_group_key(item("kinolub", "Тато", year=2021, n="3"))
    assert solo[0].key != forward[0].key


def test_group_key_from_agrees_with_merge_core() -> None:
    """#88 acceptance: ``group_key_from`` is a pure function of raw listing
    data, so a caller passing the raw year (not the effective year) gets
    a key that agrees with what ``merge_results`` would produce for that
    item in a group. If the internal ``effective_year(title, year)``
    composition is ever removed from ``group_key_from``, a caller passing
    raw ``year=None`` for "Дюна (2021)" will hash the empty year while
    ``merge_results`` still extracts 2021 from the title — the two will
    disagree, and this test fires."""
    # Raw listing data: year=None, year carried by title string.
    it = item("uakino", "Дюна (2021)", year=None)
    # The group this item joins (with a year-carrying sibling) keys on the
    # sibling's own item key — both must agree.
    sibling = item("eneyida", "Дюна", year=2021, n="2")
    group_key = merge_results([it, sibling])[0].key
    assert group_key_from(it.title, it.form, it.year, it.id) == group_key
    # Same property holds for an item alone: its item key matches the
    # singleton group's key.
    solo = merge_results([it])[0]
    assert group_key_from(it.title, it.form, it.year, it.id) == solo.key


def test_alias_bridge_limit_solo_vs_pooled_keys_differ() -> None:
    # Accepted alias-bridge limit (v3 spec §4.3): group keys are stateless
    # per-item functions, so whether an alias bridge exists depends on which
    # items happen to share a call. A SOLO "Тато" keys on its own alias,
    # while the pooled "Тато" + "Тато / Daddy" + "Daddy" merges through the
    # bridge item and keys on the group-min member instead. The solo and
    # pooled keys therefore legitimately differ — inherent to stateless
    # bridging, not a bug: no single item can know about siblings it never
    # sees, so a client's stored key for the solo title won't match the
    # pooled group's key (the group's key is still SOME member's key).
    solo = merge_results([item("kinolub", "Тато", year=2021, n="3")])
    pooled = merge_results([
        item("kinolub", "Тато", year=2021, n="3"),
        item("uakino", "Тато / Daddy", year=2021),
        item("eneyida", "Daddy", year=2021, n="2"),
    ])
    assert len(solo) == len(pooled) == 1
    assert solo[0].key == item_group_key(item("kinolub", "Тато", year=2021, n="3"))
    assert pooled[0].key in {item_group_key(m) for m in pooled[0].sources}
    assert solo[0].key != pooled[0].key


def test_merge_title_that_normalizes_to_nothing_anchors_on_item_id() -> None:
    # "(2021)" normalizes to an empty alias, so the key is anchored to the
    # item id — per-item stable, and distinct across providers.
    a = item("uakino", "(2021)", year=2021)
    b = item("eneyida", "(2021)", year=2021, n="2")
    assert item_group_key(a) != item_group_key(b)
    assert merge_results([a])[0].key == item_group_key(a)
    assert merge_results([b])[0].key == item_group_key(b)
    assert merge_results([a, b])[0].key != merge_results([a, b])[1].key


def test_year_soft_groups_do_not_collide() -> None:
    # Regression (diagnosed via code review of #69): two DIFFERENT groups
    # {Дюна 2021 + yearless} and {Дюна 1984 + yearless} both min'ed to the
    # yearless member's key sha1("дюна|movie|"), because the yearless key
    # hashed smaller than both year keys. GroupKey dedup (§3.1) would then
    # silently drop one of the titles.
    g1 = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Дюна", n="2"),  # year unknown
    ])
    g2 = merge_results([
        item("kinolub", "Дюна", year=1984, n="3"),
        item("animeua", "Дюна", n="4"),  # year unknown
    ])
    assert len(g1) == len(g2) == 1
    assert g1[0].key != g2[0].key
    # Each group keys on its year-carrying member, not the yearless one.
    assert g1[0].key == item_group_key(item("uakino", "Дюна", year=2021))
    assert g2[0].key == item_group_key(item("kinolub", "Дюна", year=1984, n="3"))


def test_transitive_year_soft_group_is_deterministic() -> None:
    # 2021 ~ yearless and 1984 ~ yearless merge transitively into one group
    # (year-soft rule, spec §4.2); its key must be one of the year-carrying
    # members' own keys — never the yearless one.
    group = merge_results([
        item("uakino", "Дюна", year=2021),
        item("eneyida", "Дюна", n="2"),
        item("kinolub", "Дюна", year=1984, n="3"),
    ])
    assert len(group) == 1
    assert group[0].key in {
        item_group_key(item("uakino", "Дюна", year=2021)),
        item_group_key(item("kinolub", "Дюна", year=1984, n="3")),
    }


def test_merge_apostrophe_variants_unify() -> None:
    groups = merge_results([
        item("uakino", "Онґелс'", year=2020),
        item("eneyida", "Онґелс’", year=2020, n="2"),
    ])
    assert len(groups) == 1


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_merge_logs_each_union_with_both_raw_titles(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cs_uk_api.merge"):
        merge_results([
            item("uakino", "Дюна", year=2021),
            item("eneyida", "Дюна (2021)", n="2"),
        ])
    merge_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("merge:")]
    assert len(merge_lines) == 1
    assert "Дюна" in merge_lines[0] and "Дюна (2021)" in merge_lines[0]
    assert "uakino" in merge_lines[0] and "eneyida" in merge_lines[0]


def test_merge_no_log_for_singletons(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cs_uk_api.merge"):
        merge_results([item("uakino", "Дюна", year=2021)])
    assert not [r for r in caplog.records if r.getMessage().startswith("merge:")]
