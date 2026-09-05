"""The Popcorn conversation seam (deepening: one client, both dialects).

``cs_uk_api.providers.popcorn.PopcornApi`` owns the English lane's ONE
upstream conversation — the YTS-v2 movie dialect AND the Popcorn-API
series dialect. These tests pin the MOVIE side of the seam directly
(the series side is exercised through the yts suite): typed records
across the seam, the no-match empty listing, pagination fields,
parse-failure guards, the loud unconfigured verdict and the card URL
grammar.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from cs_uk_api.providers.base import BaseProvider, ProviderError
from cs_uk_api.providers.popcorn import (
    MOVIE_DETAILS_PATH,
    MOVIE_LIST_PATH,
    PopcornApi,
)

_MOVIE_BASE = "https://yts.gg"


class _FakeProvider(BaseProvider):
    """Minimal composing provider lending get_json (the fetch path)."""

    id = "yts"
    name = "YTS"
    types = ("movie", "series")

    async def get_json(self, url: str, http: httpx.AsyncClient) -> object:  # type: ignore[override]
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.json()

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _client(*, movie_base: str | None = _MOVIE_BASE) -> PopcornApi:
    return PopcornApi(_FakeProvider(), base=None, movie_base=movie_base)


_MOVIE = {
    "imdb_code": "tt1375666",
    "title_english": "Inception",
    "year": 2010,
    "genres": ["Action", "Sci-Fi"],
    "medium_cover_image": "https://img.example/tt1375666.jpg",
    "rating": 8.8,
    "description_full": "A thief who steals corporate secrets.",
    "torrents": [
        {"quality": "1080p", "hash": "H1", "seeds": 100},
        {"quality": "720p", "hash": "H2"},
    ],
}


def _listing_payload(movies: object, *, count: int, limit: int) -> dict[str, object]:
    return {"status": "ok", "data": {"movie_count": count, "limit": limit, "movies": movies}}


async def test_movie_detail_returns_typed_record() -> None:
    async with respx.mock() as router:
        router.get(
            f"{_MOVIE_BASE}{MOVIE_DETAILS_PATH}", params={"imdb_id": "tt1375666"}
        ).respond(200, json={"data": {"movie": _MOVIE}})
        movie = await _client().movie("tt1375666", httpx.AsyncClient())

    assert movie.imdb == "tt1375666"
    assert movie.title == "Inception"
    assert movie.year == 2010
    assert movie.genres == ["Action", "Sci-Fi"]
    assert movie.poster == "https://img.example/tt1375666.jpg"
    assert movie.rating == 8.8
    assert movie.description == "A thief who steals corporate secrets."
    # Raw torrent entries cross the seam; candidate shaping stays policy.
    assert movie.torrents == _MOVIE["torrents"]


async def test_movies_listing_returns_typed_records_and_pagination() -> None:
    async with respx.mock() as router:
        router.get(
            f"{_MOVIE_BASE}{MOVIE_LIST_PATH}",
            params={"query_term": "incep", "limit": "50"},
        ).respond(
            200,
            json=_listing_payload([_MOVIE, {"imdb_code": "tt0816692", "title_english": "Interstellar"}], count=2, limit=50),
        )
        listing = await _client().movies(
            {"query_term": "incep", "limit": "50"}, httpx.AsyncClient()
        )

    assert [m.imdb for m in listing.movies] == ["tt1375666", "tt0816692"]
    assert listing.movie_count == 2
    assert listing.limit == 50


async def test_movies_listing_no_match_is_empty_not_parse_failure() -> None:
    """A status-ok envelope without ``movies`` is upstream's legitimate
    no-match answer → ``[]`` (ADR-0002: an empty listing is never a
    failure)."""
    async with respx.mock() as router:
        router.get(
            f"{_MOVIE_BASE}{MOVIE_LIST_PATH}",
            params={"query_term": "zzz", "limit": "50"},
        ).respond(200, json={"status": "ok", "data": {"movie_count": 0, "limit": 50}})
        listing = await _client().movies(
            {"query_term": "zzz", "limit": "50"}, httpx.AsyncClient()
        )

    assert listing.movies == []
    assert listing.movie_count == 0


async def test_movies_listing_malformed_raises_parse_failed() -> None:
    async with respx.mock() as router:
        router.get(
            f"{_MOVIE_BASE}{MOVIE_LIST_PATH}",
            params={"query_term": "x", "limit": "50"},
        ).respond(200, json={"status": "ok", "data": {"movies": "not-a-list"}})
        with pytest.raises(ProviderError) as exc:
            await _client().movies({"query_term": "x", "limit": "50"}, httpx.AsyncClient())
    assert exc.value.code == "parse_failed"


async def test_movie_detail_missing_title_raises_parse_failed() -> None:
    async with respx.mock() as router:
        router.get(
            f"{_MOVIE_BASE}{MOVIE_DETAILS_PATH}", params={"imdb_id": "tt1"}
        ).respond(200, json={"data": {"movie": {"imdb_code": "tt1"}}})
        with pytest.raises(ProviderError) as exc:
            await _client().movie("tt1", httpx.AsyncClient())
    assert exc.value.code == "parse_failed"


async def test_movie_without_configured_base_answers_loud_verdict() -> None:
    client = _client(movie_base=None)
    with pytest.raises(ProviderError) as exc:
        await client.movie("tt1375666", httpx.AsyncClient())
    assert exc.value.code == "unreachable"
    assert "movie host not configured" in exc.value.message


def test_movie_url_grammar() -> None:
    url = _client().movie_url("tt1375666")
    assert url == f"{_MOVIE_BASE}{MOVIE_DETAILS_PATH}?imdb_id=tt1375666"


async def test_movie_title_falls_back_to_title_key() -> None:
    """title_english absent → the plain ``title`` key (the fork mirror
    dialect), the same fallback the extracted code had."""
    movie = dict(_MOVIE)
    movie.pop("title_english")
    movie["title"] = "Inception"
    async with respx.mock() as router:
        router.get(
            f"{_MOVIE_BASE}{MOVIE_DETAILS_PATH}", params={"imdb_id": "tt1375666"}
        ).respond(200, json={"data": {"movie": movie}})
        result = await _client().movie("tt1375666", httpx.AsyncClient())
    assert result.title == "Inception"