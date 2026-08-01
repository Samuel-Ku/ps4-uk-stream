"""Tests for the Unimay provider (issue #17, Group 1).

Unimay (https://unimay.media) is a JSON API provider, not an HTML
scraper. The Kotlin upstream hits ``https://api.unimay.media/v1/...``
endpoints and never parses HTML, so the test suite mocks those API
endpoints with respx and asserts on the JSON shapes.
"""
from __future__ import annotations

import json
import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.unimay import UnimayProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "unimay"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _json(name: str) -> object:
    return json.loads(_fixture(name))


@pytest.mark.asyncio
async def test_unimay_search_parses_results():
    """A real search hit must yield one SearchResult per API item, with
    the external_id taken from ``code`` (not the upstream URL fragment)
    and the poster URL built from the image CDN."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"https://api\.unimay\.media/v1/release/search.*").respond(
            200, text=search_json
        )
        async with httpx.AsyncClient() as http:
            results = await UnimayProvider().search("dandadan", http)
    # Real search response for "dandadan" contains exactly 1 result.
    assert len(results) == 1
    r = results[0]
    assert r.id == "unimay:dandadan"
    assert r.provider == "unimay"
    assert r.type == "anime"
    assert "Дан Да Дан" in r.title
    assert r.poster is not None
    assert r.poster.startswith("https://img.unimay.media/")
    assert "width=640" in r.poster and "format=webp" in r.poster
    assert r.url == "https://www.unimay.media/projects/dandadan"
    assert r.year == 2024


@pytest.mark.asyncio
async def test_unimay_search_classifies_movie_results():
    """`Фільм` rows must be classified as `movie`, not `anime`.

    The search endpoint is the same as the projects listing endpoint
    (``/v1/release/search``), so we reuse the ``projects_page1.json``
    fixture (which contains both `Фільм` and `Телесеріал` rows). The
    provider must classify each item by its `type` field.
    """
    projects_json = _fixture("projects_page1.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"https://api\.unimay\.media/v1/release/search.*").respond(
            200, text=projects_json
        )
        async with httpx.AsyncClient() as http:
            results = await UnimayProvider().search("anime", http)
    by_code = {r.id.removeprefix("unimay:"): r for r in results}
    # Regression: "Фільм" rows must be `movie`, "Телесеріал" rows must be
    # `anime`. The upstream Kotlin source lumps both into `TvType.Anime`,
    # but our v2 contract separates them.
    assert by_code["suzume-locking-up-the-doors"].type == "movie"
    assert by_code["the-light-of-a-firefly-forest"].type == "movie"
    assert by_code["chainsaw-man"].type == "anime"
    assert by_code["solo-leveling"].type == "anime"


@pytest.mark.asyncio
async def test_unimay_search_handles_empty_response():
    """An empty API response must yield zero results, not raise."""
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"https://api\.unimay\.media/v1/release/search.*").respond(
            200, text='{"content":[],"totalElements":0,"last":true,"totalPages":0}'
        )
        async with httpx.AsyncClient() as http:
            results = await UnimayProvider().search("xyznothingmatches", http)
    assert results == []


@pytest.mark.asyncio
async def test_unimay_sections_lists_two():
    """Per the upstream Kotlin's `mainPageOf(...)` call, Unimay exposes
    exactly two sections: updates ("Останні релізи") and projects
    ("Наші проєкти")."""
    sections = UnimayProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["updates", "projects"]
    assert all(s.type == "anime" for s in sections)


@pytest.mark.asyncio
async def test_unimay_browse_updates_returns_15_results():
    """The "updates" section calls ``/v1/list/series/updates?size=15`` and
    returns one SearchResult per Updates item, taking the poster from
    ``release.posterUuid``. Page != 1 must return empty."""
    updates_json = _fixture("updates.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.unimay.media/v1/list/series/updates?size=15").respond(
            200, text=updates_json
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await UnimayProvider().browse("updates", 1, http)
    assert len(results) == 15
    assert all(r.provider == "unimay" for r in results)
    assert all(r.id.startswith("unimay:") for r in results)
    # Regression: updates always has has_next=False (no pagination
    # beyond page 1 per the upstream Kotlin's
    # `if (page != 1 && request.data.contains("updates"))` guard).
    assert has_next is False


@pytest.mark.asyncio
async def test_unimay_browse_updates_page2_returns_empty():
    """Per upstream: page != 1 for the updates section returns empty."""
    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.unimay.media/v1/list/series/updates?size=15").respond(
            200, text=_fixture("updates.json")
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await UnimayProvider().browse("updates", 2, http)
    assert results == []
    assert has_next is False


@pytest.mark.asyncio
async def test_unimay_browse_projects_page1_has_next_true():
    """The "projects" section paginates via ``page_size=10&page=N``.
    Page 1 contains 10 items and ``last=false`` (more pages available),
    so has_next must be True — and the test must assert the exact count."""
    p1_json = _fixture("projects_page1.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://api.unimay.media/v1/release/search?page_size=10&page=1"
        ).respond(200, text=p1_json)
        async with httpx.AsyncClient() as http:
            results, has_next = await UnimayProvider().browse("projects", 1, http)
    assert len(results) == 10
    # Regression: has_next must be True when more pages exist, not
    # `bool(...)` of the page count (caught by the code-reviewer on
    # UFDub).
    assert has_next is True


@pytest.mark.asyncio
async def test_unimay_browse_projects_page2_has_next_false():
    """Page 2 of the projects section has 6 items and ``last=true``."""
    p2_json = _fixture("projects_page2.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://api.unimay.media/v1/release/search?page_size=10&page=2"
        ).respond(200, text=p2_json)
        async with httpx.AsyncClient() as http:
            results, has_next = await UnimayProvider().browse("projects", 2, http)
    assert len(results) == 6
    assert has_next is False


@pytest.mark.asyncio
async def test_unimay_content_series_parses_title_poster_and_episodes():
    """For a `Телесеріал` row, content() must return title, poster, and
    a single season with 12 episodes. Episode ids follow
    ``unimay:<code>:<number>`` so the stream() method can locate them."""
    content_json = _fixture("release_dandadan.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.unimay.media/v1/release?code=dandadan").respond(
            200, text=content_json
        )
        async with httpx.AsyncClient() as http:
            c = await UnimayProvider().content("dandadan", http)
    assert c.id == "unimay:dandadan"
    assert c.type == "anime"
    assert "Дан Да Дан" in c.title
    assert c.year == 2024
    assert c.poster is not None
    assert c.poster.startswith("https://img.unimay.media/")
    assert "width=2560" in c.poster and "format=webp" in c.poster
    # Series must expose a season with one Episode per playlist item.
    assert c.seasons is not None
    assert len(c.seasons) == 1
    episodes = c.seasons[0].episodes
    assert len(episodes) == 12
    assert episodes[0].number == 1
    assert episodes[0].id == "unimay:dandadan:1"
    assert episodes[11].id == "unimay:dandadan:12"


@pytest.mark.asyncio
async def test_unimay_content_movie_classifies_as_movie():
    """`Фільм` rows must classify as ``movie`` (not ``anime``).

    Regression: the type-from-URL path does not apply to Unimay (there
    is no path segment distinguishing movies from series). The
    classification must come from the JSON ``type`` field."""
    content_json = _fixture("release_haikyuu_final.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.unimay.media/v1/release?code=haikyuu-final").respond(
            200, text=content_json
        )
        async with httpx.AsyncClient() as http:
            c = await UnimayProvider().content("haikyuu-final", http)
    assert c.type == "movie"
    # Movies don't have seasons.
    assert c.seasons is None


@pytest.mark.asyncio
async def test_unimay_stream_resolves_to_hls_master_url():
    """stream() must take an external_id (``<code>:<number>``), fetch
    the release, find the matching episode, and return its
    ``hls.master`` URL. Regression: ``content_id`` is NOT a URL —
    ``http.get(content_id)`` raised ``ValueError: unknown url type`` on
    every call."""
    content_json = _fixture("release_dandadan.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.unimay.media/v1/release?code=dandadan").respond(
            200, text=content_json
        )
        async with httpx.AsyncClient() as http:
            s = await UnimayProvider().stream("dandadan:1", None, http)
    assert s.url.startswith("https://api.unimay.media/v1/hls/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # The upstream passes the main page as Referer.
    assert s.headers["Referer"] == "https://www.unimay.media/"


@pytest.mark.asyncio
async def test_unimay_stream_for_movie_uses_playlist_index_zero():
    """Movies have a single playlist entry. The upstream `loadLinks`
    only takes the episode number from the data field — for movies the
    client passes ``<code>:1`` and we still resolve playlist[0]."""
    content_json = _fixture("release_haikyuu_final.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.unimay.media/v1/release?code=haikyuu-final").respond(
            200, text=content_json
        )
        async with httpx.AsyncClient() as http:
            s = await UnimayProvider().stream("haikyuu-final:1", None, http)
    assert s.url.startswith("https://api.unimay.media/v1/hls/")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_unimay_browse_unknown_section_raises():
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await UnimayProvider().browse("nonexistent", 1, httpx.AsyncClient())