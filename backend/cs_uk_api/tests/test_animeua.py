"""Tests for the AnimeUA provider (issue #17, Group 1).

AnimeUA (https://animeua.club) is a DLE HTML site; episodes live on
ashdi.vip player pages whose inline script holds ``file: '[...]'`` —
either a JSON array of dubs (series) or a direct m3u8 URL (films).
All fixtures are real bytes captured 2026-08-01: the search used the
query "dandadan" (exact POST fields: do=search, subaction=search,
story=dandadan), the content page is ``/7952-dandadan.html`` and the
player page is ``https://ashdi.vip/serial/3942``.
"""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.models import Translation
from cs_uk_api.providers.animeua import AnimeUAProvider
from cs_uk_api.providers.base import ProviderError

FIX = pathlib.Path(__file__).parent / "fixtures" / "animeua"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_animeua_search_parses_real_response():
    """Search POST with the upstream fields must yield one SearchResult
    per `a.poster` card, with the provider prefix on the ids."""
    with respx.mock(assert_all_called=True) as router:
        router.post("https://animeua.club/").respond(200, text=_fixture("search.html"))
        async with httpx.AsyncClient() as http:
            results = await AnimeUAProvider().search("dandadan", http)
    assert len(results) == 2
    assert results[0].id == "animeua:8079-dandadan-2"
    assert results[0].provider == "animeua"
    assert "anime" in results[0].styles
    assert results[0].title == "Дандадан 2"
    assert results[0].url == "https://animeua.club/8079-dandadan-2.html"
    assert results[0].poster == "https://animeua.club/uploads/posts/2025-08/3a43204e424cfba2f1d29df67940cccd.webp"
    assert results[1].id == "animeua:7952-dandadan"


@pytest.mark.asyncio
async def test_animeua_sections_match_upstream_main_page():
    """The section list must match the upstream `mainPageOf(...)` calls:
    page/, film/page/, anime/page/, ona/page/, ova/page/."""
    sections = AnimeUAProvider().sections
    assert [s.id for s in sections] == ["page", "film", "anime", "ona", "ova"]
    assert [s.title for s in sections] == ["Нове аніме", "Повнометражки", "Аніме серіали", "ONA", "OVA"]
    # Contract #135: sections carry Model B axes (style wins, else form).
    assert [(s.form, sorted(s.styles or [])) for s in sections] == [
        (None, ["anime"]), ("movie", []), (None, ["anime"]), (None, ["anime"]), (None, ["anime"]),
    ]


@pytest.mark.asyncio
async def test_animeua_series_parses_seasons_and_episode_translations():
    """A `/serial/` player must classify as anime and build one Season
    per dub-season, with per-episode translations = the dubs that carry
    that episode. Season 2 drops the dubs that only cover season 1."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeua.club/7952-dandadan.html").respond(200, text=_fixture("content.html"))
        router.get("https://ashdi.vip/serial/3942").respond(200, text=_fixture("player.html"))
        async with httpx.AsyncClient() as http:
            content = await AnimeUAProvider().content("7952-dandadan", http)
    assert content.id == "animeua:7952-dandadan"
    assert "anime" in content.styles
    # Model B axes: a `/serial/` player = anime series → form=series
    # with the anime style (not the plain-series default).
    assert content.form == "series"
    assert content.styles == frozenset({"anime"})
    assert content.title == "Дандадан"
    assert content.year == 2024
    assert content.poster == "https://animeua.club/uploads/posts/2024-10/5458831_1730282979.webp"
    assert content.description
    assert content.translations_level == "episode"
    assert content.seasons is not None
    assert len(content.seasons) == 2
    assert len(content.seasons[0].episodes) == 12
    assert len(content.seasons[1].episodes) == 12
    first = content.seasons[0].episodes[0]
    assert first.id == "animeua:7952-dandadan:s1e1"
    assert first.title == "Серія 1"
    assert first.translations is not None
    assert len(first.translations) == 10
    names = [t.id for t in first.translations]
    assert "FanVoxUA" in names
    assert "Студія Качур" in names
    assert "субтитри | AniKappa" in names
    s2_first = content.seasons[1].episodes[0]
    assert s2_first.id == "animeua:7952-dandadan:s2e1"
    assert len(s2_first.translations or []) == 7


@pytest.mark.asyncio
async def test_animeua_episode_translations_round_trip():
    """episode_translations() must return exactly the dubs available for
    the requested episode — the same list that content() attached."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeua.club/7952-dandadan.html").respond(200, text=_fixture("content.html"))
        router.get("https://ashdi.vip/serial/3942").respond(200, text=_fixture("player.html"))
        async with httpx.AsyncClient() as http:
            provider = AnimeUAProvider()
            allowed = await provider.episode_translations("7952-dandadan:s2e1", http)
    assert allowed is not None
    assert len(allowed) == 7
    assert "Робота Голосом" in allowed


@pytest.mark.asyncio
async def test_animeua_stream_resolves_episode_m3u8():
    """stream() must rebuild the content page -> player page chain and
    return the episode's m3u8 with the upstream tortuga Referer."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeua.club/7952-dandadan.html").respond(200, text=_fixture("content.html"))
        router.get("https://ashdi.vip/serial/3942").respond(200, text=_fixture("player.html"))
        async with httpx.AsyncClient() as http:
            stream = await AnimeUAProvider().stream("7952-dandadan:s1e1", None, http)
    assert stream.url == "https://ashdi.vip/video01/2/serials/fanvoxua_dandadan/dandadan01online_144735/hls/Da+Xjn6RkuZVhAb3/index.m3u8"
    assert stream.type == "m3u8"
    assert stream.headers["Referer"] == "https://tortuga.wtf/"


@pytest.mark.asyncio
async def test_animeua_stream_selects_requested_dub_and_season():
    """A translation id must select that dub's file for the episode, and
    season 2 must resolve against the season-2 folders."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeua.club/7952-dandadan.html").respond(200, text=_fixture("content.html"))
        router.get("https://ashdi.vip/serial/3942").respond(200, text=_fixture("player.html"))
        async with httpx.AsyncClient() as http:
            stream = await AnimeUAProvider().stream("7952-dandadan:s2e1", "Студія Качур", http)
    assert stream.url == "https://ashdi.vip/video01/3/serials/kachur_dandadan/dandadan_1_serya_2_sezon_187510/hls/Da+Xjn6RkuZVhAb3/index.m3u8"
    assert stream.type == "m3u8"


@pytest.mark.asyncio
async def test_animeua_search_non_200_raises_upstream_unreachable():
    with respx.mock(assert_all_called=True) as router:
        router.post("https://animeua.club/").respond(500, text="boom")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as excinfo:
                await AnimeUAProvider().search("dandadan", http)
    assert excinfo.value.code == "upstream_unreachable"


@pytest.mark.asyncio
async def test_animeua_stream_missing_player_raises_parse_failed():
    html = "<html><body><div class='page__subcol-main'><h1>X</h1></div></body></html>"
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeua.club/7952-dandadan.html").respond(200, text=html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as excinfo:
                await AnimeUAProvider().stream("7952-dandadan:s1e1", None, http)
    assert excinfo.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_animeua_series_with_dead_player_keeps_default_translation():
    """A series whose player page exposes no playable file must not crash
    with an empty translations list — content stays readable with the
    default Ukrainian track."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeua.club/7952-dandadan.html").respond(200, text=_fixture("content.html"))
        router.get("https://ashdi.vip/serial/3942").respond(200, text="<html>no player json</html>")
        async with httpx.AsyncClient() as http:
            content = await AnimeUAProvider().content("7952-dandadan", http)
    assert content.translations_level == "content"
    assert content.translations == [Translation(id="uk", label="Українська")]
    assert content.seasons is None


@pytest.mark.asyncio
async def test_animeua_content_bad_external_id_raises_not_found():
    r"""Regression: `content()` must reject external_ids that do not
    match the `\d+-[a-z0-9-]+` slug regex before interpolating into
    the URL — otherwise a caller-supplied `../../etc/passwd` would
    escape the upstream URL path."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeUAProvider().content("../../etc/passwd", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_animeua_stream_bad_content_id_raises_not_found():
    """Regression: `stream()` strips any `s<N>e<M>` suffix and
    rebuilds the content URL from the embedded external_id; reject
    payload that escapes the slug charset before the first HTTP call."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeUAProvider().stream("../../etc/passwd", None, http)
    assert exc.value.code == "not_found"
