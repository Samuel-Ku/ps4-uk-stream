"""The facade RESOLUTION half (deepening: the lookup side of the split).

``cs_uk_api.jellyfin.resolution`` is the lookup/grammar half the router
assembles from — view-id round trips, the include-types and genre-ids
parsers, the season-suffix and episode-wire-id grammar, user-data
lookup and the snapshot-derived library counts. These tests exercise
the module's OWN surface (the interface is the test surface); the wire
behaviour is pinned by the route-level suites.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from cs_uk_api.jellyfin import resolution as res


def test_view_id_round_trips_through_row_kinds() -> None:
    for kind in ("recent_movie", "popular", "movie", "anime"):
        view_id = res._view_id_for(kind)
        assert res._view_type_by_id(view_id) == kind
        # Deterministic across calls (client-cached library lists
        # survive restarts — D5).
        assert res._view_id_for(kind) == view_id


def test_view_id_is_32_hex_shape() -> None:
    for kind in ("popular", "genre:action", "recipe:personalized"):
        assert len(res._view_id_for(kind)) == 32
        assert int(res._view_id_for(kind), 16) >= 0


def test_parse_include_types_none_absent_empty_when_unexpressible() -> None:
    assert res._parse_include_types(None) is None
    assert res._parse_include_types("Movie,Series") is not None
    # A type we express nothing for filters everything out.
    assert res._parse_include_types("Book") == set()


def test_parse_genre_ids_round_trips_names() -> None:
    assert res._parse_genre_ids(None) is None
    assert res._parse_genre_ids("action, thriller") == {"action", "thriller"}
    assert res._parse_genre_ids(" , ") == set()


def test_split_season_suffix_grammar() -> None:
    assert res._split_season_suffix("g2:abc") == ("g2:abc", None)
    assert res._split_season_suffix("g2:abc:S2") == ("g2:abc", 2)
    assert res._split_season_suffix("g2:abc:S10") == ("g2:abc", 10)
    # A non-group id is returned as-is (episode wire ids, view ids).
    assert res._split_season_suffix("yts:tt1:s1e2") == ("yts:tt1:s1e2", None)


def test_episode_wire_id_prefix_only_when_missing() -> None:
    assert res._episode_wire_id("uakino", "uakino:123:s1e2") == "uakino:123:s1e2"
    assert res._episode_wire_id("kinotron", "abc:s1e2") == "kinotron:abc:s1e2"


def test_snapshot_counts_zero_when_cold() -> None:
    """Cold snapshot → honest zeros, never a fetch (spec #280)."""
    counts = res._snapshot_counts()
    assert counts.MovieCount == 0
    assert counts.SeriesCount == 0
    assert counts.EpisodeCount == 0