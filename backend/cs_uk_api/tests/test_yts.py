"""Tests for the YTS English-movies provider (spec #374, ticket #376).

Fixture provenance: every payload is JSON synthesized in the verified
live response shape from research #366 (`gh gist view
d86d3abec3a063e7f04eb70e432eac47`, live probes 2026-08-25). The
«Gekijô-ban …» newest-listing entry carries the live probe's REAL
field values (imdb_code ``tt33050528``, torrent hash
``D546E07722C3014C2D9244312E59EBA841A5DB19``, size/date) with its
truncated display fields reconstructed; the Dune entries use their
real IMDb codes. No live calls happen in CI.

Scope note (recorded on ticket #376): this pass is MOVIES ONLY. The
research found the Popcorn SERIES hosts dead; series acceptance
criteria are deferred, not faked.
"""

from __future__ import annotations

import json
import pathlib
import re

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.yts import (
    TorrentCandidate,
    YtsProvider,
    _torrent_candidates,
    build_magnet,
    select_torrent,
)

FIX = pathlib.Path(__file__).parent / "fixtures" / "yts"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _list_url_regex() -> re.Pattern[str]:
    return re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")


def _details_url_regex() -> re.Pattern[str]:
    return re.compile(r"https://yts\.gg/api/v2/movie_details\.json\?.*")


# ---------------------------------------------------------------------------
# Provider surface
# ---------------------------------------------------------------------------


def test_yts_provider_metadata():
    """Movies-only surface: id/name/types, one movie section that is also
    the newest listing (home composition picks it up later), and BOTH API
    hosts declared so the central allowlist admits the in-flight base
    migration redirect (research #366 §5)."""
    p = YtsProvider()
    assert p.id == "yts"
    assert p.name == "YTS"
    assert p.types == ("movie",)
    assert [s.id for s in p.sections] == ["movies"]
    assert p.newest_section == "movies"
    assert p.allowed_hosts == frozenset({"yts.gg", "movies-api.accel.li"})


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yts_search_parses_real_response():
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=_list_url_regex()).respond(
            200, text=_fixture("search_dune.json")
        )
        async with httpx.AsyncClient() as http:
            results = await YtsProvider().search("dune", http)
    assert len(results) == 2
    first = results[0]
    assert first.id == "yts:tt1160419"
    assert first.provider == "yts"
    assert first.title == "Dune"
    assert first.year == 2021
    assert first.form == "movie"
    assert first.styles == frozenset()
    assert first.poster is not None and first.poster.startswith("https://yts.gg/")
    assert "Sci-Fi" in first.genres
    # Endpoint contract: query_term carries the query, limit pinned.
    request = route.calls[0].request
    assert request.url.params["query_term"] == "dune"
    assert request.url.params["limit"] == "50"


@pytest.mark.asyncio
async def test_yts_search_no_match_returns_empty_not_failure():
    """A no-match query answers status ok WITHOUT a movies key — a
    legitimate empty result (never surfaced in /api/search failures)."""
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_list_url_regex()).respond(
            200, text=_fixture("search_no_match.json")
        )
        async with httpx.AsyncClient() as http:
            results = await YtsProvider().search("zzz-no-such-title", http)
    assert results == []


@pytest.mark.asyncio
async def test_yts_search_malformed_payload_raises_parse_failed():
    """A non-object data envelope must surface as a typed parse error,
    not crash or silently return []."""
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_list_url_regex()).respond(
            200, text=_fixture("malformed_list.json")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().search("dune", http)
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_yts_search_connection_error_raises_unreachable():
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_list_url_regex()).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().search("dune", http)
    assert exc.value.code == "unreachable"


# ---------------------------------------------------------------------------
# browse (newest section)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yts_browse_newest_page1_has_next():
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=_list_url_regex()).respond(
            200, text=_fixture("newest_page1.json")
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await YtsProvider().browse("movies", 1, http)
    assert len(results) == 2
    assert all(r.form == "movie" for r in results)
    assert all(r.styles == frozenset() for r in results)
    # The verbatim live-probed entry keeps its stable IMDb external id.
    assert results[0].id == "yts:tt33050528"
    assert has_next is True
    request = route.calls[0].request
    assert request.url.params["sort_by"] == "date_added"
    assert request.url.params["page"] == "1"


@pytest.mark.asyncio
async def test_yts_browse_last_page_has_no_next():
    """76799 titles at limit 50 end exactly on page 1536 — the boundary
    math (count <= page * limit) must flip has_next off."""
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_list_url_regex()).respond(
            200, text=_fixture("newest_end.json")
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await YtsProvider().browse("movies", 1536, http)
    assert len(results) == 2
    assert has_next is False


@pytest.mark.asyncio
async def test_yts_browse_unknown_section_raises_not_found():
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().browse("series", 1, http)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yts_content_parses_movie_with_sentinel_episode():
    """English originals carry NO dubs — the ContentResponse mirrors the
    single-audio movie pattern of the Ukrainian providers: ONE Translation
    entry (id = language code), and the canonical movie sentinel episode
    id feeding the wire grammar."""
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=_details_url_regex()).respond(
            200, text=_fixture("details_tt1160419.json")
        )
        async with httpx.AsyncClient() as http:
            content = await YtsProvider().content("tt1160419", http)
    assert content.id == "yts:tt1160419"
    assert content.form == "movie"
    assert content.title == "Dune"
    assert content.year == 2021
    assert content.description.startswith("Paul Atreides")
    assert content.poster is not None and content.poster.startswith("https://yts.gg/")
    assert content.styles == frozenset()
    assert content.rating == 8.0
    assert "Sci-Fi" in content.genres
    assert len(content.translations) == 1
    assert content.translations[0].id == "en"
    assert content.seasons is not None
    episodes = content.seasons[0].episodes
    assert len(episodes) == 1
    assert episodes[0].id == "yts:tt1160419:__movie__"
    request = route.calls[0].request
    assert request.url.params["imdb_id"] == "tt1160419"


@pytest.mark.asyncio
async def test_yts_content_malformed_payload_raises_parse_failed():
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_details_url_regex()).respond(
            200, text=_fixture("malformed_details.json")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().content("tt1160419", http)
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_yts_content_bad_imdb_rejected_before_request():
    r"""Boundary validation: only ``tt\d{7,8}`` may reach the URL —
    anything else is rejected before the first HTTP call."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().content("../../etc/passwd", http)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# stream placeholder + torrent payload threading (#377 consumption)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yts_stream_unconfigured_engine_is_loud(monkeypatch):
    """#377 replaced the placeholder: stream() now demands an engine.
    With none injected and settings unconfigured, the verdict is a LOUD
    typed ``unreachable`` — never silence, never a pretend stream."""
    import cs_uk_api.config as config_mod
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(
        config_mod, "SETTINGS", dc_replace(config_mod.SETTINGS, torrent_engine_url=None)
    )
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().stream("tt1160419:__movie__", None, http)
    assert exc.value.code == "unreachable"
    assert "not configured" in exc.value.message


@pytest.mark.asyncio
async def test_yts_torrents_embedded_in_details_threaded_for_playback():
    """The torrents[] array comes embedded in the details payload; the
    quality→hash map stays threaded on the provider instance so #377 can
    build magnets without a second upstream call."""
    p = YtsProvider()
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_details_url_regex()).respond(
            200, text=_fixture("details_tt1160419.json")
        )
        async with httpx.AsyncClient() as http:
            await p.content("tt1160419", http)
    assert p.torrent_hashes("tt1160419") == {
        "720p": "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678",
        "1080p": "B2C3D4E5F60718293A4B5C6D7E8F90123456789A",
    }
    # Unknown ids answer empty, never raise.
    assert p.torrent_hashes("tt0000000") == {}


@pytest.mark.asyncio
async def test_yts_torrents_also_threaded_from_listing_payload():
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_list_url_regex()).respond(
            200, json=json.loads(_fixture("search_dune.json"))
        )
        async with httpx.AsyncClient() as http:
            p = YtsProvider()
            await p.search("dune", http)
    assert p.torrent_hashes("tt15239678") == {
        "1080p": "C3D4E5F60718293A4B5C6D7E8F90123456789ABC",
    }


# ---------------------------------------------------------------------------
# Magnet→session selection policy (#377): pure, deterministic, single pick
# ---------------------------------------------------------------------------


def _cand(quality: str, info_hash: str, seeds: int) -> TorrentCandidate:
    return TorrentCandidate(quality=quality, info_hash=info_hash, seeds=seeds)


def test_select_torrent_quality_dominates_seed_count():
    """The ordering key is (quality tier, seeds): 1080p wins even with a
    seed deficit — the decided v1 preference is 1080p > 720p > others."""
    picked = select_torrent(
        [
            _cand("720p", "H720", 5000),
            _cand("1080p", "H1080", 3),
        ]
    )
    assert picked == _cand("1080p", "H1080", 3)


def test_select_torrent_720p_beats_unlisted_qualities():
    picked = select_torrent(
        [
            _cand("480p", "H480", 900),
            _cand("720p", "H720", 10),
            _cand("3D", "H3D", 400),
        ]
    )
    assert picked == _cand("720p", "H720", 10)


def test_select_torrent_more_seeds_win_within_same_quality():
    """Repack/encode variants share a quality: the best-seeded one is the
    single server-side pick (spec #374 quality policy)."""
    picked = select_torrent(
        [
            _cand("1080p", "H_REPACK", 7),
            _cand("1080p", "H_HOT", 250),
            _cand("1080p", "H_COLD", 42),
        ]
    )
    assert picked == _cand("1080p", "H_HOT", 250)


def test_select_torrent_first_entry_wins_on_full_tie():
    picked = select_torrent(
        [
            _cand("1080p", "H_FIRST", 100),
            _cand("1080p", "H_SECOND", 100),
        ]
    )
    assert picked == _cand("1080p", "H_FIRST", 100)


def test_select_torrent_unknown_qualities_form_one_tier_seeds_decide():
    """Everything outside 1080p/720p shares the last tier — among them,
    seeds decide (2160p is deliberately NOT preferred in v1)."""
    picked = select_torrent(
        [
            _cand("480p", "H_LOW", 1000),
            _cand("2160p", "H_UHD", 2000),
        ]
    )
    assert picked == _cand("2160p", "H_UHD", 2000)


def test_select_torrent_empty_input_picks_nothing():
    assert select_torrent([]) is None


def test_build_magnet_carries_infohash_and_fork_tracker_list():
    magnet = build_magnet("B2C3D4E5F60718293A4B5C6D7E8F90123456789A")
    assert magnet.startswith("magnet:?xt=urn:btih:B2C3D4E5F60718293A4B5C6D7E8F90123456789A")
    # The fork's tracker convention rides along so the engine's peer
    # discovery has public fallbacks beyond the DHT bootstrap.
    assert "&tr=" in magnet
    assert "udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce" in magnet


def test_parse_candidates_reads_quality_hash_and_seeds():
    movie = {
        "torrents": [
            {"quality": "1080p", "hash": "H_A", "seeds": 50},
            {"quality": "1080p", "hash": "H_B", "seeds": 200},
            {"quality": "720p", "hash": "H_C"},  # no seeds key → 0
            {"quality": "720p", "hash": 123},  # non-string hash skipped
            {"quality": "", "hash": "H_D"},  # empty quality skipped
            "not-a-dict",
        ]
    }
    cands = _torrent_candidates(movie)
    assert cands == [
        _cand("1080p", "H_A", 50),
        _cand("1080p", "H_B", 200),
        _cand("720p", "H_C", 0),
    ]
