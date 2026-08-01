"""Tests for per-episode translations (issue #9)."""
from __future__ import annotations

from pydantic import ValidationError

from cs_uk_api.models import (
    ContentResponse,
    Episode,
    Season,
    Translation,
)


def test_episode_optional_translations_default_none():
    ep = Episode(number=1, id="x:s1e1", title="Ep 1")
    assert ep.translations is None


def test_episode_can_carry_translations():
    ep = Episode(
        number=1,
        id="x:s1e1",
        title="Ep 1",
        translations=[
            Translation(id="uk", label="Українська"),
            Translation(id="sub", label="Субтитри"),
        ],
    )
    assert ep.translations is not None
    assert [t.id for t in ep.translations] == ["uk", "sub"]


def test_content_response_translation_level_default_is_content():
    cr = ContentResponse(
        id="x:y",
        type="series",
        title="X",
        translations=[Translation(id="uk", label="Українська")],
        seasons=[Season(number=1, episodes=[Episode(number=1, id="x:s1e1", title="E1")])],
        translation_level="content",
    )
    assert cr.translation_level == "content"


def test_content_response_translation_level_can_be_episode():
    cr = ContentResponse(
        id="x:y",
        type="series",
        title="X",
        translations=[Translation(id="uk", label="Українська")],
        translation_level="episode",
    )
    assert cr.translation_level == "episode"


def test_content_response_rejects_unknown_translation_level():
    import pytest

    with pytest.raises(ValidationError):
        ContentResponse(
            id="x:y",
            type="series",
            title="X",
            translations=[Translation(id="uk", label="Українська")],
            translation_level="bogus",  # type: ignore[arg-type]
        )


def test_translations_field_present_on_round_trip():
    """Pydantic round-trips the new fields through dict()/model_dump."""
    cr = ContentResponse(
        id="x:y",
        type="anime",
        title="Naruto",
        translations=[Translation(id="uk", label="Українська")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(
                        number=1,
                        id="x:s1e1",
                        title="Ep 1",
                        translations=[
                            Translation(id="dub", label="Дубляж"),
                            Translation(id="sub", label="Субтитри"),
                        ],
                    )
                ],
            )
        ],
        translation_level="episode",
    )
    dumped = cr.model_dump()
    assert dumped["translation_level"] == "episode"
    assert dumped["seasons"][0]["episodes"][0]["translations"][0]["id"] == "dub"