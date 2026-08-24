"""Row-kind registry consistency (spec #323 Row T1 #329, #362 D1).

AC #329/#362: one table is the single source of row-kind facts; every
home row kind has an entry; every entry maps on the wire; adding a row
kind touches the table only. These tests pin the table's invariants —
the canonical home-emission order, the wire mappings (view id /
CollectionType / Jellyfin Type), the form filter, the sources selector
and the extendability split — plus cross-module facts so a facade or
builder divergence from the table cannot recur (spec #362 hardening).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import cs_uk_api
from cs_uk_api import home
from cs_uk_api.home import section_row_type
from cs_uk_api.models import MediaForm, MediaStyle, SearchResult, Section
from cs_uk_api.recommend import LLM_IDEA_ROW_TYPES
from cs_uk_api.row_kinds import (
    KINDS_BY_JF_TYPE,
    ROW_KINDS,
    TYPE_KINDS,
    VIEW_TYPE_BY_ID,
    RowKind,
    item_matches_row,
)

#: The home-row routing keys that can exist in a snapshot and their
#: canonical home-emission order (spec #362 D1): the form-split recent
#: rows → «Нові серії» → «Нещодавно переглянуто» → «Популярні зараз» →
#: the five type rows → the LLM idea slots.
HOME_KINDS = (
    "recent_movie",
    "recent_series",
    "new_episodes",
    "recently_watched",
    "popular",
    "movie",
    "series",
    "anime",
    "cartoon",
    "dorama",
    "llm_idea_1",
    "llm_idea_2",
)
TYPE_KINDS_ORDER = ("movie", "series", "anime", "cartoon", "dorama")
FORM_FILTERED_KINDS = ("recent_movie", "recent_series", "new_episodes", "movie", "series")


def _item(form: MediaForm) -> SearchResult:
    return SearchResult(
        id="x",
        provider="p",
        form=form,
        title="T",
        url="u",
    )


# ---------------------------------------------------------------------------
# AC1: one table is the single source of row-kind facts
# ---------------------------------------------------------------------------

def test_every_home_kind_has_a_registry_entry() -> None:
    assert set(HOME_KINDS) == set(ROW_KINDS)
    # The retired «Новинки» kind is gone from the table (spec #263/#362).
    assert "newest" not in ROW_KINDS


def test_table_order_is_the_home_order() -> None:
    # Insertion order IS the canonical home-emission order — the
    # builder's row sequence and the derived TYPE_KINDS both flow from
    # it.
    assert tuple(ROW_KINDS) == HOME_KINDS


def test_type_kinds_are_the_five_type_rows_in_spec_order() -> None:
    assert TYPE_KINDS == TYPE_KINDS_ORDER


def test_entry_kind_matches_its_key() -> None:
    for kind, entry in ROW_KINDS.items():
        assert entry.kind == kind


def test_human_titles() -> None:
    assert [ROW_KINDS[k].title for k in HOME_KINDS] == [
        "Нещодавно додані: Фільми",
        "Нещодавно додані: Серіали",
        "Нові серії",
        "Нещодавно переглянуто",
        "Популярні зараз",
        "Фільми",
        "Серіали",
        "Аніме",
        "Мультфільми",
        "Дорами",
        "Ідея",
        "Ідея",
    ]


def test_section_axes_derive_only_into_registry_kinds() -> None:
    # Every axis value the section derivation can produce
    # (home.section_row_type) is a registry type-kind — a section can
    # never bucket into a kind the table doesn't know.
    form_axes: list[MediaForm] = ["movie", "series"]
    style_axes: list[MediaStyle] = ["anime", "cartoon", "dorama"]
    for form_axis in form_axes:
        section = Section(id="s", title="S", form=form_axis, styles=frozenset())
        kind = section_row_type(section)
        assert kind is not None
        assert kind in ROW_KINDS
        assert ROW_KINDS[kind].sources == "type"
    for style_axis in style_axes:
        section = Section(
            id="s", title="S", form=None, styles=frozenset({style_axis})
        )
        kind = section_row_type(section)
        assert kind is not None
        assert kind in ROW_KINDS
        assert ROW_KINDS[kind].sources == "type"


# ---------------------------------------------------------------------------
# AC: every entry maps on the wire (view id / CollectionType / JF Type)
# ---------------------------------------------------------------------------

def test_every_entry_has_a_unique_reversible_view_id() -> None:
    ids = [rk.view_id for rk in ROW_KINDS.values()]
    # 32-hex uuid5 (no dashes), unique per kind.
    assert len(ids) == len(set(ids))
    assert all(len(i) == 32 and all(c in "0123456789abcdef" for c in i) for i in ids)
    # Reversible: VIEW_TYPE_BY_ID round-trips every kind.
    assert set(VIEW_TYPE_BY_ID) == set(ids)
    for kind, rk in ROW_KINDS.items():
        assert VIEW_TYPE_BY_ID[rk.view_id] == kind


def test_view_id_matches_the_deterministic_formula() -> None:
    for kind, rk in ROW_KINDS.items():
        assert rk.view_id == uuid.uuid5(
            uuid.NAMESPACE_URL, f"cs-uk-api-view:{kind}"
        ).hex


def test_reverse_wire_map_covers_every_kind_exactly_once() -> None:
    buckets = list(KINDS_BY_JF_TYPE.values())
    flat = [k for b in buckets for k in b]
    assert sorted(flat) == sorted(ROW_KINDS)
    assert all(rk.jf_type in KINDS_BY_JF_TYPE for rk in ROW_KINDS.values())


def test_include_item_types_form_behavior_is_preserved() -> None:
    # The genres route filters by ``item.form in want_type``. The
    # reverse map is a superset of the old index for both Types, but the
    # extra kinds never appear as an item's form — so the wire filter
    # outcome is identical: Movie admits only movies, Series admits only
    # series.
    assert KINDS_BY_JF_TYPE["Movie"] == {"movie", "recent_movie"}
    assert KINDS_BY_JF_TYPE["Series"] >= {
        "series",
        "anime",
        "cartoon",
        "dorama",
    }
    for form in ("movie", "series"):
        series_kinds = KINDS_BY_JF_TYPE["Series"]
        assert (form in series_kinds) == (form == "series")


# ---------------------------------------------------------------------------
# AC: form filter / sources selector / extendability
# ---------------------------------------------------------------------------

def test_form_filter_invariant() -> None:
    # A "form"-filtered row carries a form axis; an "any" row carries
    # None. The form-filtered kinds are exactly movie/series plus the
    # three newest-sourced series/movie-form rows (spec #362).
    assert tuple(k for k in HOME_KINDS if ROW_KINDS[k].filter == "form") == (
        FORM_FILTERED_KINDS
    )
    for kind, rk in ROW_KINDS.items():
        if kind in FORM_FILTERED_KINDS:
            assert rk.filter == "form"
            assert rk.form is not None
        else:
            assert rk.filter == "any"
            assert rk.form is None
    # Each form-filtered row admits exactly its own axis.
    assert ROW_KINDS["recent_movie"].form == "movie"
    assert ROW_KINDS["recent_series"].form == "series"
    assert ROW_KINDS["new_episodes"].form == "series"


def test_item_matches_row_reads_the_table_filter() -> None:
    # Form rows admit only their own form; any rows admit everything.
    assert item_matches_row("movie", _item("movie"))
    assert not item_matches_row("movie", _item("series"))
    assert item_matches_row("series", _item("series"))
    assert not item_matches_row("series", _item("movie"))
    assert item_matches_row("recent_movie", _item("movie"))
    assert not item_matches_row("recent_movie", _item("series"))
    assert item_matches_row("recent_series", _item("series"))
    assert not item_matches_row("recent_series", _item("movie"))
    assert item_matches_row("new_episodes", _item("series"))
    assert not item_matches_row("new_episodes", _item("movie"))
    any_rows = (
        "anime",
        "cartoon",
        "dorama",
        "popular",
        "recently_watched",
        "llm_idea_1",
        "llm_idea_2",
    )
    for kind in any_rows:
        assert item_matches_row(kind, _item("movie"))
        assert item_matches_row(kind, _item("series"))


def test_sources_selector_split() -> None:
    # The sources selector splits the table five ways (spec #362 D2):
    # the form-split recent rows off the providers' newest listings,
    # «Популярні зараз» off the popular browse, «Нещодавно переглянуто»
    # off the playback history, the LLM idea slots off the profile's
    # curated ideas, and the five type rows off the by-type fan-out.
    by_sources: dict[str, set[str]] = {}
    for kind, rk in ROW_KINDS.items():
        by_sources.setdefault(rk.sources, set()).add(kind)
    assert by_sources == {
        "newest": {"recent_movie", "recent_series", "new_episodes"},
        "history": {"recently_watched"},
        "popular": {"popular"},
        "idea": {"llm_idea_1", "llm_idea_2"},
        "type": set(TYPE_KINDS),
    }
    assert [k for k, rk in ROW_KINDS.items() if rk.sources == "type"] == list(
        TYPE_KINDS
    )


def test_extendability_split() -> None:
    # Deep-rows (#305): the type rows, the form-split recent rows and
    # «Популярні зараз» page past the snapshot (popular adopts the
    # shipped deep-row behaviour — spec #362); the personalized rows,
    # the idea slots stay snapshot-bounded.
    extendable = {k for k, rk in ROW_KINDS.items() if rk.extendable}
    assert extendable == set(TYPE_KINDS) | {"recent_movie", "recent_series", "popular"}
    assert not ROW_KINDS["new_episodes"].extendable
    assert not ROW_KINDS["recently_watched"].extendable
    assert not ROW_KINDS["llm_idea_1"].extendable
    assert not ROW_KINDS["llm_idea_2"].extendable


# ---------------------------------------------------------------------------
# Cross-module facts (spec #362 hardening item 3): the private builder
# tuples must agree with the table so divergence cannot recur
# ---------------------------------------------------------------------------

def test_llm_idea_slots_are_table_kinds() -> None:
    assert set(LLM_IDEA_ROW_TYPES) <= set(ROW_KINDS)


def test_home_recent_row_tuples_match_the_table() -> None:
    for form, label, kind in home._RECENT_ROWS:
        assert kind in ROW_KINDS
        assert ROW_KINDS[kind].title == label
        assert ROW_KINDS[kind].form == form
    label, kind = home._NEW_EPISODES_ROW
    assert ROW_KINDS[kind].title == label
    label, kind = home._RECENTLY_WATCHED_ROW
    assert ROW_KINDS[kind].title == label


def test_warm_insert_scan_kinds_are_table_kinds() -> None:
    """The recommendation insert-scan tuple in ``_catalog_state/warm.py``
    names only table kinds — the retired «Новинки» zombie is gone from
    the scan (spec #362 D)."""
    text = (
        Path(cs_uk_api.__file__).parent / "_catalog_state" / "warm.py"
    ).read_text(encoding="utf-8")
    m = re.search(r"row\.type in \(([^)]*)\)", text)
    assert m is not None, "insert-scan tuple not found in warm.py"
    scan = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert scan <= set(ROW_KINDS)
    assert scan == {"recent_movie", "recent_series", "popular"}


# ---------------------------------------------------------------------------
# RowKind is immutable — the table can't be mutated by accident
# ---------------------------------------------------------------------------

def test_entries_are_immutable() -> None:
    assert all(isinstance(rk, RowKind) for rk in ROW_KINDS.values())
    for rk in ROW_KINDS.values():
        # Frozen dataclass: attribute reassignment is blocked.
        try:
            rk.title = "mutated"  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("RowKind should be frozen")
