"""Row-kind registry consistency (spec #323, Row T1 #329).

AC #329: one table is the single source of row-kind facts; every home
row kind has an entry; every entry maps on the wire; adding a row kind
touches the table only. These tests pin the table's invariants — the
home order, the wire mappings (view id / CollectionType / Jellyfin
Type), the form filter, the sources selector and the extendability
split — so a future row-kind edit can't silently desync the facade or
the home builder.
"""

from __future__ import annotations

import uuid

from cs_uk_api.home import section_row_type
from cs_uk_api.models import MediaForm, MediaStyle, SearchResult, Section
from cs_uk_api.row_kinds import (
    KINDS_BY_JF_TYPE,
    ROW_KINDS,
    TYPE_KINDS,
    VIEW_TYPE_BY_ID,
    RowKind,
    item_matches_row,
)

#: The home-row routing keys that can exist in a snapshot (v3 spec §3.1)
#: and their spec-mandated home order: «Новинки» → «Популярні зараз» →
#: the five type rows.
HOME_KINDS = ("newest", "popular", "movie", "series", "anime", "cartoon", "dorama")
TYPE_KINDS_ORDER = ("movie", "series", "anime", "cartoon", "dorama")


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


def test_table_order_is_the_home_order() -> None:
    # Insertion order IS the home order — the builder's row sequence and
    # the derived TYPE_KINDS both flow from it.
    assert tuple(ROW_KINDS) == HOME_KINDS


def test_type_kinds_are_the_five_type_rows_in_spec_order() -> None:
    assert TYPE_KINDS == TYPE_KINDS_ORDER


def test_entry_kind_matches_its_key() -> None:
    for kind, entry in ROW_KINDS.items():
        assert entry.kind == kind


def test_human_titles() -> None:
    assert ROW_KINDS["newest"].title == "Новинки"
    assert ROW_KINDS["popular"].title == "Популярні зараз"
    assert [ROW_KINDS[k].title for k in TYPE_KINDS_ORDER] == [
        "Фільми",
        "Серіали",
        "Аніме",
        "Мультфільми",
        "Дорами",
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


def test_wire_mappings_match_the_retired_facade_vocabularies() -> None:
    # Parity with the pre-registry maps (_JF_TYPE_BY_ROW /
    # _COLLECTION_TYPE_BY_ROW): movie is the only Movie/movies row,
    # everything else is Series/tvshows.
    for kind, rk in ROW_KINDS.items():
        assert rk.jf_type == ("Movie" if kind == "movie" else "Series")
        assert rk.collection_type == ("movies" if kind == "movie" else "tvshows")


def test_reverse_wire_map_covers_every_kind_exactly_once() -> None:
    buckets = list(KINDS_BY_JF_TYPE.values())
    flat = [k for b in buckets for k in b]
    assert sorted(flat) == sorted(ROW_KINDS)
    assert all(rk.jf_type in KINDS_BY_JF_TYPE for rk in ROW_KINDS.values())


def test_include_item_types_form_behavior_is_preserved() -> None:
    # The genres route filters by ``item.form in want_type``. The
    # reverse map is a superset of the old index for Series, but the
    # extra kinds (newest/popular) never appear as an item's form — so
    # the wire filter outcome is identical: Movie admits only movies,
    # Series admits only series.
    assert KINDS_BY_JF_TYPE["Movie"] == {"movie"}
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
    # None. The form-filtered kinds are exactly movie/series.
    for kind, rk in ROW_KINDS.items():
        if kind in ("movie", "series"):
            assert rk.filter == "form"
            assert rk.form == kind
        else:
            assert rk.filter == "any"
            assert rk.form is None


def test_item_matches_row_reads_the_table_filter() -> None:
    # Form rows admit only their own form; any rows admit everything.
    assert item_matches_row("movie", _item("movie"))
    assert not item_matches_row("movie", _item("series"))
    assert item_matches_row("series", _item("series"))
    assert not item_matches_row("series", _item("movie"))
    for kind in ("anime", "cartoon", "dorama", "newest", "popular"):
        assert item_matches_row(kind, _item("movie"))
        assert item_matches_row(kind, _item("series"))


def test_sources_selector_split() -> None:
    # Exactly two personalized rows (newest_section / popular browse)
    # and five type rows (by-type section fan-out).
    assert [k for k, rk in ROW_KINDS.items() if rk.sources == "type"] == list(
        TYPE_KINDS
    )
    assert {k for k, rk in ROW_KINDS.items() if rk.sources != "type"} == {
        "newest",
        "popular",
    }


def test_extendability_split() -> None:
    # Deep-rows (#305 AC4): personalized rows stay snapshot-bounded;
    # the type rows are the ones the extension pages.
    assert [k for k, rk in ROW_KINDS.items() if rk.extendable] == list(TYPE_KINDS)
    assert not ROW_KINDS["newest"].extendable
    assert not ROW_KINDS["popular"].extendable


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
