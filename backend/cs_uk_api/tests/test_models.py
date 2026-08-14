import pytest
from pydantic import ValidationError

from cs_uk_api.models import ContentResponse, SearchResponse, SearchResult, StreamResponse


def test_search_result_round_trip():
    r = SearchResult(
        id="uakino:1",
        provider="uakino",
        form="movie",
        title="Дюна",
        year=2021,
        poster="/api/poster?u=abc",
        url="https://uakino.club/film/1",
    )
    assert r.id == "uakino:1"
    assert r.model_dump()["form"] == "movie"


def test_search_query_length_is_validated():
    with pytest.raises(ValidationError):
        SearchResponse(query="", groups=[])


def test_content_movie_requires_translations():
    with pytest.raises(ValidationError):
        ContentResponse.model_validate(
            {
                "id": "uakino:1",
                "form": "movie",
                "title": "X",
                "year": 2020,
                "description": "",
                "poster": "/api/poster?u=abc",
                "translations": [],
            }
        )


def test_stream_response_defaults():
    s = StreamResponse(url="https://cdn/film.mp4", type="mp4")
    assert s.headers == {}
