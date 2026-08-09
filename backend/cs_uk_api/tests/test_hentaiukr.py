"""Tests for the HentaiUkr provider (issue #17, Group 4, NSFW in scope).

The upstream Kotlin source (HentaiUkrProvider.kt, 5.3 KB) drives the
site by fetching a single JSON manifest at
``https://hentaiukr.com/search/objects.json`` and reading the
``video`` array. The manifest is used for both browse and search —
search is a case-insensitive substring filter on ``it.name``.

The content page (``/video/<slug>/``) carries the title, year, and
poster as DOM hooks (``#name-ukr``, ``#year``, ``#img-placeholder img``)
and the episode list lives in a separate JSON file at
``<content_url>plur.cfg.json``. Each entry in that JSON is one episode
with multiple MP4 sources (1080/720/480); the upstream adds all sources
to the extractor, so for v2 we pick the highest-quality source.
"""
from __future__ import annotations

import json
import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.hentaiukr import HentaiUkrProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "hentaiukr"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_hentaiukr_search_filters_by_name_substring():
    """Regression: upstream search uses `it.name.contains(query, true)`
    (case-insensitive substring on the Ukrainian name), not on
    ``eng_name`` or ``orig_name``. Query "Наґай" returns exactly the
    one video with that name."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://hentaiukr.com/search/objects.json").respond(
            200, text=search_json
        )
        async with httpx.AsyncClient() as http:
            results = await HentaiUkrProvider().search("Наґай", http)
    assert len(results) == 1
    r = results[0]
    assert r.provider == "hentaiukr"
    assert "Наґай" in r.title
    # ID is "hentaiukr:<id>" where <id> is the upstream `id` (integer)
    # — encoded as a string. We do NOT use the URL slug because the
    # upstream itself uses the integer `id` for episodes (see
    # `loadLinks(data.split(", ")[1].toInt())`).
    assert r.id == "hentaiukr:159"
    assert r.type == "anime"
    assert r.poster is not None
    assert r.poster.startswith("https://hentaiukr.com")
    assert r.url == "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/"


@pytest.mark.asyncio
async def test_hentaiukr_search_returns_multiple_matches():
    """A query with multiple hits must return all of them. The upstream
    treats the entire `video` array as the searchable corpus."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://hentaiukr.com/search/objects.json").respond(
            200, text=search_json
        )
        async with httpx.AsyncClient() as http:
            results = await HentaiUkrProvider().search("хоч", http)
    # Real objects.json fixture: query "хоч" returns 6 hits (every
    # title containing the Cyrillic substring "хоч").
    assert len(results) == 6
    assert {r.id for r in results} == {
        "hentaiukr:155",
        "hentaiukr:154",
        "hentaiukr:94",
        "hentaiukr:63",
        "hentaiukr:21",
        "hentaiukr:2",
    }


@pytest.mark.asyncio
async def test_hentaiukr_search_classifies_all_results_as_anime():
    """Every row on HentaiUkr is NSFW anime/hentai; the upstream maps
    every item to ``TvType.NSFW``. Per our v2 contract NSFW collapses
    to ``anime`` (this is the only media type the provider exposes)."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://hentaiukr.com/search/objects.json").respond(
            200, text=search_json
        )
        async with httpx.AsyncClient() as http:
            results = await HentaiUkrProvider().search("хоч", http)
    assert all(r.type == "anime" for r in results)


@pytest.mark.asyncio
async def test_hentaiukr_browse_returns_full_video_listing():
    """Browse fetches the same JSON as search but returns the full
    `video` array (no substring filter). The real objects.json fixture
    carries 139 video entries. The provider exposes a single section
    "Хентай" and reports ``has_next=False`` because the upstream
    JSON is a flat list with no pagination cursor."""
    objects_json = _fixture("objects.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://hentaiukr.com/search/objects.json").respond(
            200, text=objects_json
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await HentaiUkrProvider().browse("hentai", 1, http)
    assert len(results) == 139
    assert has_next is False


@pytest.mark.asyncio
async def test_hentaiukr_browse_unknown_section_raises():
    """An unknown section id must raise ProviderError('not_found') so
    the API returns 404 instead of leaking an empty page. The provider
    should validate the section id *before* hitting the manifest, so
    we don't register the objects.json route here."""
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError) as exc_info:
            await HentaiUkrProvider().browse("does-not-exist", 1, httpx.AsyncClient())
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_hentaiukr_sections_lists_one():
    """Per the upstream Kotlin ``mainPageOf(objectsUrl to "Хентай")``
    — exactly one section."""
    sections = HentaiUkrProvider().sections
    assert len(sections) == 1
    assert sections[0].id == "hentai"
    assert sections[0].title == "Хентай"
    assert sections[0].type == "anime"


def _objects_route(router: "respx.MockRouter") -> None:
    """Mock the upstream ``/search/objects.json`` manifest.

    The provider looks up the URL slug from this manifest every time
    ``content()`` or ``stream()`` is called (the upstream Kotlin does
    the same — it stores the slug alongside the integer id on the
    ``SearchResponse`` it returns from ``search()``). The integer id
    alone does not encode the URL slug, so the manifest is mandatory.
    """
    router.get("https://hentaiukr.com/search/objects.json").respond(
        200, text=_fixture("objects.json")
    )


@pytest.mark.asyncio
async def test_hentaiukr_content_parses_title_year_poster():
    """The content page carries the Ukrainian title in ``#name-ukr``,
    the year in ``#year`` ("Рік виходу : 2025"), and the poster in
    ``#img-placeholder img`` / ``#img``. The upstream uses
    ``#name-ukr`` for the title, so we must not return the
    ``<title>`` (which has the "| Хентай аніме українською" suffix)."""
    content_html = _fixture("content_video_159.html")
    cfg_json = _fixture("plur_cfg_159.json")
    with respx.mock(assert_all_called=True) as router:
        _objects_route(router)
        router.get(
            "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/"
        ).respond(200, text=content_html)
        router.get(
            "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/plur.cfg.json"
        ).respond(200, text=cfg_json)
        async with httpx.AsyncClient() as http:
            c = await HentaiUkrProvider().content("159", http)
    assert "Наґай" in c.title
    assert "Хентай" not in c.title  # no "| Хентай..." suffix
    assert c.type == "anime"
    assert c.year == 2025
    assert c.poster is not None
    assert c.poster.startswith("https://hentaiukr.com")
    assert len(c.translations) >= 1


@pytest.mark.asyncio
async def test_hentaiukr_content_loads_episodes_from_plur_cfg_json():
    """The episode list is loaded from ``<content_url>plur.cfg.json``;
    each entry in that JSON is one episode with multiple MP4 sources
    (1080/720/480). The provider must hit both URLs and surface the
    episode count."""
    content_html = _fixture("content_video_159.html")
    cfg_json = _fixture("plur_cfg_159.json")
    with respx.mock(assert_all_called=True) as router:
        _objects_route(router)
        router.get(
            "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/"
        ).respond(200, text=content_html)
        router.get(
            "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/plur.cfg.json"
        ).respond(200, text=cfg_json)
        async with httpx.AsyncClient() as http:
            c = await HentaiUkrProvider().content("159", http)
    # plur.cfg_159.json contains 3 entries, each with sources 1080/720/480
    # → 3 episodes in season 1.
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 3
    titles = [ep.title for ep in c.seasons[0].episodes]
    assert titles == ["Серія 1", "Серія 2", "Серія 3"]
    # Episode ids are prefixed with the provider namespace (issue #175)
    # so /api/stream can route on the first ':'. 1-based episode index.
    assert c.seasons[0].episodes[0].id == "hentaiukr:159:1"
    assert c.seasons[0].episodes[2].id == "hentaiukr:159:3"


@pytest.mark.asyncio
async def test_hentaiukr_stream_picks_highest_quality_source():
    """REGRESSION: ``content_id`` is the upstream integer id plus the
    episode index (no URL!). The provider must rebuild the URL
    internally and pick the highest-quality source (1080 > 720 > 480)
    rather than the first one in the list. plur_cfg_159.json orders
    sources 1080, 720, 480 — the highest-quality must win.

    The upstream ``loadLinks()`` only fetches ``plur.cfg.json`` (the
    URL + episode index already came from ``load()``), so we do not
    register a content-page route here.
    """
    cfg_json = _fixture("plur_cfg_159.json")
    with respx.mock(assert_all_called=True) as router:
        _objects_route(router)
        router.get(
            "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/plur.cfg.json"
        ).respond(200, text=cfg_json)
        async with httpx.AsyncClient() as http:
            # Pass content_id as "159:1" (id=159, episode=1) — NOT a URL.
            s = await HentaiUkrProvider().stream("159:1", None, http)
    assert s.type == "mp4"
    assert s.url == (
        "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/1080/01.mp4"
    )


@pytest.mark.asyncio
async def test_hentaiukr_stream_picks_best_quality_when_1080_missing():
    """If a release has no 1080 source, the provider must pick the
    next best (720 > 480). plur_cfg_158.json lists only ``720/1.mp4``
    per episode — that should be the resolved URL."""
    cfg_json = _fixture("plur_cfg_158.json")
    with respx.mock(assert_all_called=True) as router:
        _objects_route(router)
        router.get(
            "https://hentaiukr.com/video/158_seksual_ni_vpodobannja/plur.cfg.json"
        ).respond(200, text=cfg_json)
        async with httpx.AsyncClient() as http:
            s = await HentaiUkrProvider().stream("158:1", None, http)
    assert s.type == "mp4"
    assert s.url == (
        "https://hentaiukr.com/video/158_seksual_ni_vpodobannja/720/1.mp4"
    )


@pytest.mark.asyncio
async def test_hentaiukr_stream_unknown_episode_raises_not_found():
    """REGRESSION (KinoTron): if the requested episode index is out
    of range, ``stream`` must raise ``not_found`` — never silently
    fall back to the first available episode."""
    cfg_json = _fixture("plur_cfg_159.json")
    with respx.mock(assert_all_called=True) as router:
        _objects_route(router)
        router.get(
            "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/plur.cfg.json"
        ).respond(200, text=cfg_json)
        with pytest.raises(ProviderError) as exc_info:
            async with httpx.AsyncClient() as http:
                await HentaiUkrProvider().stream("159:99", None, http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_hentaiukr_stream_bare_id_defaults_to_first_episode():
    """Live-gate regression (2026-08-01): search results carry a bare
    id (``hentaiukr:113``, no episode index), and the gate streams
    straight from search. A bare id must resolve to episode 1 — the
    upstream Kotlin's default index — instead of raising
    ``bad content_id``."""
    cfg_json = _fixture("plur_cfg_159.json")
    with respx.mock(assert_all_called=True) as router:
        _objects_route(router)
        router.get(
            "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/plur.cfg.json"
        ).respond(200, text=cfg_json)
        async with httpx.AsyncClient() as http:
            s = await HentaiUkrProvider().stream("159", None, http)
    assert s.type == "mp4"
    assert s.url == (
        "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/1080/01.mp4"
    )


@pytest.mark.asyncio
async def test_hentaiukr_stream_rejects_url_as_content_id():
    """REGRESSION (UFDub): ``stream()`` receives the external_id
    (``"159:1"``), NOT a URL. Passing a URL must not crash; it must
    be parsed as ``<id>:<episode>`` and (because the URL contains a
    slash that is not a colon) raise not_found."""
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await HentaiUkrProvider().stream(
                "https://hentaiukr.com/video/159_velychezni_tsyts_ky_nagaj/",
                None,
                httpx.AsyncClient(),
            )


@pytest.mark.asyncio
async def test_hentaiukr_search_handles_missing_query():
    """The upstream's ``quickSearch`` invokes ``search(query)`` with
    an arbitrary string. An empty / no-match query returns an empty
    list rather than raising — clients depend on this for the
    "typeahead" UX in main.py."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://hentaiukr.com/search/objects.json").respond(
            200, text=search_json
        )
        async with httpx.AsyncClient() as http:
            results = await HentaiUkrProvider().search("zzz_no_match_qq", http)
    assert results == []