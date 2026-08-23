"""Unit tests for the Jellyfin DTO mapping layer (#344).

Domain object -> wire dict assertions against ``jellyfin/dto`` pure
functions; the route-level suites cover the same shapes end-to-end.
"""

from __future__ import annotations

from cs_uk_api.jellyfin import dto
from cs_uk_api.models import Episode, Season


def test_poster_tag_is_stable_and_non_empty() -> None:
    tag = dto.poster_tag("https://cdn.example/poster.jpg")
    assert tag
    assert tag == dto.poster_tag("https://cdn.example/poster.jpg")


def test_user_data_none_without_item_id() -> None:
    assert dto.user_data(None, favorite=True, played=True) is None


def test_user_data_played_without_position() -> None:
    result = dto.user_data("g2:abc", favorite=False, played=True)
    assert result is not None
    assert result.Played is True
    assert result.IsFavorite is False
    assert result.PlayedPercentage == 100.0
    assert result.PlayCount == 1


def test_user_data_position_percentage_capped_at_100() -> None:
    result = dto.user_data(
        "g2:abc",
        favorite=False,
        played=False,
        position_ticks=150,
        runtime_ticks=100,
    )
    assert result is not None
    assert result.PlaybackPositionTicks == 150
    assert result.PlayedPercentage == 100.0
    assert result.PlayCount == 0


def test_user_data_position_percentage_rounded() -> None:
    result = dto.user_data(
        "g2:abc",
        favorite=True,
        played=False,
        position_ticks=50,
        runtime_ticks=60,
    )
    assert result is not None
    assert result.PlayedPercentage == round(50 / 60 * 100, 2)
    assert result.IsFavorite is True


def test_row_dto_is_collection_folder() -> None:
    item = dto.row_dto(
        "Новинки", "server-1", view_id="view-uuid", collection_type="movies"
    )
    assert item.Type == "CollectionFolder"
    assert item.Name == "Новинки"
    assert item.Id == "view-uuid"
    assert item.CollectionType == "movies"
    assert item.ServerId == "server-1"


def test_episode_dto_maps_hierarchy_fields() -> None:
    season = Season(
        number=2,
        episodes=[Episode(number=7, id="raw", title="Епізод 7")],
    )
    episode = season.episodes[0]
    item = dto.episode_dto(
        "g2:abc",
        season,
        episode,
        "server-1",
        "Серіал",
        wire_id="uakino:6268:s2e7",
        user_data_value=None,
    )
    assert item.Type == "Episode"
    assert item.Id == "uakino:6268:s2e7"
    assert item.ParentId == "g2:abc:S2"
    assert item.SeriesId == "g2:abc"
    assert item.SeriesName == "Серіал"
    assert item.IndexNumber == 7
    assert item.ParentIndexNumber == 2
    assert item.Name == "Епізод 7"
    assert item.UserData is None


def test_season_dto_shapes_a_season_satellite() -> None:
    season = Season(
        number=1,
        episodes=[Episode(number=1, id="raw", title="Епізод 1")],
    )
    item = dto.season_dto("g2:abc", season, "server-1", "Серіал")
    assert item.Type == "Season"
    assert item.ServerId == "server-1"
    assert item.SeriesName == "Серіал"


def test_safe_filename_strips_path_separators() -> None:
    name = dto.safe_filename('Серіал: сезон 1/пілот?"')
    assert name
    assert "/" not in name
    assert '"' not in name
