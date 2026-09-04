"""YTS series pass — seasons to episode playback (#379, spec #374).

The English lane's serial path: the Popcorn-API series host (configured
via ``CS_UK_POPCORN_BASE_URL`` — every known public host is dead,
research #366; the knob points at a self-hosted popcorn-api or a live
mirror) supplies shows/seasons/episodes with per-episode quality maps;
the provider turns ONE season into the v2 ContentResponse whose episode
ids ride the canonical ``:sNeM`` wire grammar, and ``stream()`` resolves
an episode id through the SAME engine handoff as a movie — one policy
pick for the season's torrent, the season number as the engine's
``file_hint`` so the adapter's deterministic file selection lands on the
right file inside a multi-file season pack.

Covered at this layer (fixture provenance: payloads synthesized in the
research #366 grammar with real IMDb codes — no live calls in CI):
- ``content()`` duality: popcorn configured → the series envelope;
  unconfigured → the original #376 movies envelope, byte-identical.
- ``series_content(season)`` per-season envelopes; season 2 → the
  deterministic ``not_found`` (an empty season never caches).
- ``stream()`` on ``yts:<imdb>:sNeM``: magnet or verbatim-url
  identifiers, season-derived file hint, engine DI + loud unconfigured
  verdict, taxonomy mapping.
- IMDb-derived external id stability (the resume contract): the ids a
  listing produced are replayable after upstream churn (new fixture
  payload, SAME imdb id ⇒ same episode wire ids).
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
    YtsProvider,
)
from cs_uk_api.torrent_engine import (
    EngineRejected,
    EngineStream,
    EngineUnavailable,
    FakeTorrentEngine,
)
from cs_uk_api.wire_identity import (
    episode_wire_id,
    split_episode_tail,
    split_wire_id,
)

FIX = pathlib.Path(__file__).parent / "fixtures" / "yts"

_POPCORN = "http://popcorn.lan:9000"
_MAGNET_720 = "magnet:?xt=urn:btih:2233445566778899AABBCCDDEEFF001122334455&dn=chernobyl.s01e01.720p&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
_MAGNET_480 = "magnet:?xt=urn:btih:00112233445566778899AABBCCDDEEFF00112233&dn=chernobyl.s01e01.480p&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"

_SHOWS_URL = re.compile(rf"{re.escape(_POPCORN)}/shows/\d+\?.*")
_SHOW_URL = re.compile(rf"{re.escape(_POPCORN)}/show/tt\d+")
_S1E1 = episode_wire_id("yts", "tt8740758", 1, 1)
_LAN = EngineStream(
    url="http://bitplay.lan:3347/api/v1/torrent/s01/stream/2", container="mp4"
)
_S1E2 = episode_wire_id("yts", "tt8740758", 1, 2)
_S1E3 = episode_wire_id("yts", "tt8740758", 1, 3)


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _configured(monkeypatch: pytest.MonkeyPatch, base: str = _POPCORN) -> None:
    from dataclasses import replace as dc_replace

    import cs_uk_api.config as config_mod

    monkeypatch.setattr(
        config_mod, "SETTINGS", dc_replace(config_mod.SETTINGS, popcorn_base_url=base)
    )


def _unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace as dc_replace

    import cs_uk_api.config as config_mod

    monkeypatch.setattr(
        config_mod, "SETTINGS", dc_replace(config_mod.SETTINGS, popcorn_base_url=None)
    )


# ---------------------------------------------------------------------------
# content() duality: series envelope vs the #376 movies envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_series_envelope_when_popcorn_configured(monkeypatch):
    """A bare ``yts:<imdb>`` id resolves to the SEASON-1 series envelope:
    form=series, canonical ``:sNeM`` episode wire ids, English-only
    translation, the show's metadata."""
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            content = await YtsProvider().content("tt8740758", http)
    assert content.id == "yts:tt8740758"
    assert content.form == "series"
    assert content.title == "Chernobyl"
    assert content.year == 2019
    assert content.poster is not None and content.poster.startswith("https://pic.timstvshows.nl/")
    assert "drama" in content.genres
    assert content.rating == pytest.approx(95.0)
    assert content.styles == frozenset()
    assert content.translations_level == "content"
    assert len(content.translations) == 1
    assert content.translations[0].id == "en"
    seasons = content.seasons
    assert seasons is not None and len(seasons) == 1
    assert seasons[0].number == 1
    assert [e.id for e in seasons[0].episodes] == [_S1E1, _S1E2, _S1E3]
    assert seasons[0].episodes[0].title == "1:23:45"
    assert "explosion" in seasons[0].episodes[0].description


@pytest.mark.asyncio
async def test_content_movies_envelope_when_popcorn_unconfigured(monkeypatch):
    """The #376 contract holds byte-for-byte when the series host is
    NOT configured: movie envelope on the same external id, sentinel
    episode id, the Dune fixture's torrents threaded."""
    _unconfigured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=re.compile(r"https://yts\.gg/api/v2/movie_details\.json\?.*")).respond(
            200, text=_fixture("details_tt1160419.json")
        )
        async with httpx.AsyncClient() as http:
            content = await YtsProvider().content("tt1160419", http)
    assert content.form == "movie"
    assert content.seasons is not None
    assert content.seasons[0].episodes[0].id == "yts:tt1160419:__movie__"


# ---------------------------------------------------------------------------
# series_content(season): per-season envelopes + deterministic boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_series_content_season1_full_rail(monkeypatch):
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            content = await YtsProvider().series_content("tt8740758", 1, http)
    seasons = content.seasons
    assert seasons is not None and seasons[0].number == 1
    assert [e.number for e in seasons[0].episodes] == [1, 2, 3]


@pytest.mark.asyncio
async def test_series_content_season2_envelope(monkeypatch):
    """Multi-season shows: the season rail asks by number, the envelope
    carries exactly that season's episodes."""
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            content = await YtsProvider().series_content("tt8740758", 2, http)
    seasons = content.seasons
    assert seasons is not None and seasons[0].number == 2
    assert [e.id for e in seasons[0].episodes] == [episode_wire_id("yts", "tt8740758", 2, 1)]


@pytest.mark.asyncio
async def test_series_content_unknown_season_is_deterministic_not_found(monkeypatch):
    """A season the show's episodes[] never mentions stays a typed
    not_found (an empty season is never a silent empty listing)."""
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().series_content("tt8740758", 3, http)
    assert exc.value.code == "not_found"
    assert "no season 3" in exc.value.message


@pytest.mark.asyncio
async def test_series_content_malformed_payload_raises_parse_failed(monkeypatch):
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_malformed.json"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().series_content("tt8740758", 1, http)
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_series_content_unconfigured_lane_is_loud(monkeypatch):
    """A season rail for an unconfigured lane answers the LOUD typed
    verdict — never silence, never a pretend-empty season."""
    _unconfigured(monkeypatch)
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().series_content("tt8740758", 1, http)
    assert exc.value.code == "unreachable"
    assert "not configured" in exc.value.message


# ---------------------------------------------------------------------------
# series search + browse (Popcorn /shows pages)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_merges_series_results_when_configured(monkeypatch):
    """With the series host configured, search = the YTS movies call
    PLUS the Popcorn shows call; this payload's movies answer is the
    legitimate empty listing, so the series rows are all of it."""
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")).respond(
            200, text=_fixture("search_no_match.json")
        )
        shows = router.get(url=_SHOWS_URL).respond(
            200, text=_fixture("series_search_money_heist.json")
        )
        async with httpx.AsyncClient() as http:
            results = await YtsProvider().search("heist", http)
    assert [r.id for r in results] == ["yts:tt6468322", "yts:tt8740758"]
    assert all(r.form == "series" for r in results)
    assert all(r.styles == frozenset() for r in results)
    first = results[0]
    assert first.title == "Money Heist"
    assert first.year == 2017
    assert "crime" in first.genres
    request = shows.calls.last.request
    assert request.url.params["sort"] == "name"
    assert request.url.params["keywords"] == "heist"


@pytest.mark.asyncio
async def test_search_movies_only_when_unconfigured(monkeypatch):
    """The movies lane NEVER depends on the series host: unconfigured ⇒
    search = the plain YTS movies call (no series fetch attempted)."""
    _unconfigured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")).respond(
            200, text=_fixture("search_dune.json")
        )
        async with httpx.AsyncClient() as http:
            results = await YtsProvider().search("dune", http)
    assert len(results) == 2
    assert route.call_count == 1
    assert not any(r.form == "series" for r in results)


@pytest.mark.asyncio
async def test_search_series_host_flake_degrades_to_movies(monkeypatch):
    """The series host flaking must not sink the movies lane: the series
    fetch raises, search logs and degrades to the movies-only result."""
    _configured(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        movies = router.get(url=re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")).respond(
            200, text=_fixture("search_dune.json")
        )
        router.get(url=_SHOWS_URL).mock(side_effect=httpx.ConnectError("mirror down"))
        async with httpx.AsyncClient() as http:
            results = await YtsProvider().search("dune", http)
    assert movies.call_count == 1
    assert [r.form for r in results] == ["movie", "movie"]


@pytest.mark.asyncio
async def test_browse_series_page_and_has_next(monkeypatch):
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=_SHOWS_URL).respond(200, text=_fixture("series_page2_last.json"))
        async with httpx.AsyncClient() as http:
            results, has_next = await YtsProvider().browse("series", 2, http)
    assert [r.id for r in results] == ["yts:tt6468322", "yts:tt8740758"]
    assert has_next is False  # 2 < the 50-item full-page bar
    request = route.calls[0].request
    assert request.url.params["sort"] == "updated"
    assert request.url.params["order"] == "-1"


@pytest.mark.asyncio
async def test_browse_series_unconfigured_is_loud(monkeypatch):
    _unconfigured(monkeypatch)
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().browse("series", 1, http)
    assert exc.value.code == "unreachable"


@pytest.mark.asyncio
async def test_popcorn_host_joins_allowlist_when_configured(monkeypatch):
    """The configured series host is declared to the provider's
    allowlist (ADR-0005) so the central SSRF check admits its fetches;
    the YTS hosts stay declared alongside."""
    _configured(monkeypatch)
    p = YtsProvider()
    assert "popcorn.lan:9000" in p.allowed_hosts
    assert "yts.gg" in p.allowed_hosts
    assert "movies-api.accel.li" in p.allowed_hosts


# ---------------------------------------------------------------------------
# stream(): episode wire id → season torrent → engine session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_episode_picks_policy_winner_and_passes_season_hint(monkeypatch):
    """Two quality variants across the season's episodes: the policy
    lands on 720p/610-seeds; the engine receives the VERBATIM magnet
    (never hash-rebuilt) and the season file hint."""
    _configured(monkeypatch)
    lan = EngineStream(url="http://bitplay.lan:3347/api/v1/torrent/s01/stream/2", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_720: lan})
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            resp = await YtsProvider(engine=engine).stream(_S1E1, None, http)
    assert resp.url == lan.url
    assert resp.type == "mp4"
    assert resp.headers == {}
    assert engine.ensure_count == 1
    assert engine.last_identifier == _MAGNET_720
    assert engine.last_file_hint == "s01e"


@pytest.mark.asyncio
async def test_stream_episode2_rides_same_season_torrent(monkeypatch):
    """Both episodes of the pack share the season's torrent table: the
    SAME policy winner (same magnet), the same season hint — the file
    discrimination happens inside the engine."""
    _configured(monkeypatch)
    lan = EngineStream(url="http://bitplay.lan:3347/api/v1/torrent/s01/stream/3", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_720: lan})
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            await YtsProvider(engine=engine).stream(_S1E2, None, http)
    assert engine.last_identifier == _MAGNET_720
    assert engine.last_file_hint == "s01e"


@pytest.mark.asyncio
async def test_stream_torrentless_episode_rides_season_pack(monkeypatch):
    """Episode 3 itself carries an EMPTY torrent map, but the SEASON's
    merged table has the pack — the pack semantics mean the episode
    streams like its siblings (the engine's file selection finds its
    file inside the pack)."""
    _configured(monkeypatch)
    lan = EngineStream(url="http://bitplay.lan:3347/api/v1/torrent/s01/stream/4", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_720: lan})
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            resp = await YtsProvider(engine=engine).stream(_S1E3, None, http)
    assert resp.url == lan.url
    assert engine.last_identifier == _MAGNET_720


@pytest.mark.asyncio
async def test_stream_episode_of_season_without_torrents_is_not_found(monkeypatch):
    """An episode id whose season has NO episodes/torrents at all answers
    the deterministic ``not_found`` (no session is ensured)."""
    _configured(monkeypatch)
    engine = FakeTorrentEngine()
    s3e1 = episode_wire_id("yts", "tt8740758", 3, 1)
    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider(engine=engine).stream(s3e1, None, http)
    assert exc.value.code == "not_found"
    assert "season 3" in exc.value.message
    assert engine.ensure_count == 0


@pytest.mark.asyncio
async def test_stream_episode_bad_wire_id_rejected_before_everything(monkeypatch):
    """A bare id (no tail), a movie-suffix id on a SHOW external, or a
    malformed tail is rejected at the wire grammar — no network, no
    engine."""
    _configured(monkeypatch)
    engine = FakeTorrentEngine()
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            p = YtsProvider(engine=engine)
            for bad in ("tt8740758", "yts:tt8740758", "yts:tt8740758:e1", "yts:../../etc:__movie__"):
                with pytest.raises(ProviderError) as exc:
                    await p.stream(bad, None, http)
                assert exc.value.code == "not_found"
    assert engine.ensure_count == 0


@pytest.mark.asyncio
async def test_stream_episode_engine_unavailable_maps_to_unreachable(monkeypatch):
    _configured(monkeypatch)
    class _Exploding:
        async def ensure_session(self, identifier, *, file_hint=None):
            raise EngineUnavailable("connection refused")

    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider(engine=_Exploding()).stream(_S1E1, None, http)
    assert exc.value.code == "unreachable"


@pytest.mark.asyncio
async def test_stream_episode_engine_rejected_maps_to_not_found(monkeypatch):
    """Zero seeders / metadata timeout keeps ITS deterministic verdict on
    the episode lane too — distinct from the flaky-infra code above."""
    _configured(monkeypatch)
    class _Exploding:
        async def ensure_session(self, identifier, *, file_hint=None):
            raise EngineRejected("504 metadata timeout")

    with respx.mock(assert_all_called=True) as router:
        router.get(url=_SHOW_URL).respond(200, text=_fixture("series_show_tt8740758.json"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider(engine=_Exploding()).stream(_S1E1, None, http)
    assert exc.value.code == "not_found"
    assert exc.value.message == "no seeders or dead torrent"


# ---------------------------------------------------------------------------
# IMDb-derived id stability (the resume/user-state contract, spec #374)
# ---------------------------------------------------------------------------


def test_episode_wire_ids_survive_upstream_churn():
    """The wire id is a pure function of (provider, imdb, season, ep) —
    a listing re-parse under changed upstream metadata (new slug, new
    poster, re-ranked torrents) yields the SAME ids."""
    ids_before = {ep_id for ep_id in (_S1E1, _S1E2, _S1E3)}
    reparsed = {
        episode_wire_id("yts", "tt8740758", season, ep)
        for season, ep in ((1, 1), (1, 2), (1, 3))
    }
    assert reparsed == ids_before
    # The grammar also round-trips through the canonical tail split the
    # resume reverse-lookup uses.
    composite, tail = split_episode_tail(_S1E1)
    assert split_wire_id(composite) == ("yts", "tt8740758")
    assert tail == ":s1e1"


@pytest.mark.asyncio
async def test_stream_after_churn_refetches_same_imdb_ids(monkeypatch):
    """Replay after upstream churn: a NEW payload (same IMDb id) drives
    the same episode wire ids through content(), and the stream route
    resolves them (ids survive the listing refresh — the resume pin)."""
    _configured(monkeypatch)
    churned = json.loads(_fixture("series_show_tt8740758.json"))
    churned["title"] = "Chernobyl (re-uploaded)"
    churned["episodes"][0]["torrents"]["720p"]["seeds"] = 9_999
    lan = EngineStream(url="http://lan/after-churn", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_720: lan})
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=_SHOW_URL).respond(200, text=json.dumps(churned))
        async with httpx.AsyncClient() as http:
            p = YtsProvider(engine=engine)
            content = await p.content("tt8740758", http)
            assert content.seasons is not None
            assert [e.id for e in content.seasons[0].episodes] == [_S1E1, _S1E2, _S1E3]
            resp = await p.stream(_S1E1, None, http)
    assert resp.url == lan.url
    assert route.call_count == 1  # content + stream share the TTL-cached show fetch


@pytest.mark.asyncio
async def test_playback_chain_costs_one_show_fetch(monkeypatch):
    """Acceptance (lean session slice): PlaybackInfo → stream → VTT-class
    resolution for ONE episode = exactly ONE Popcorn show fetch. The
    engine's BitPlay session dedups by infohash, so the provider's
    upstream conversation is the cost that matters."""
    _configured(monkeypatch)
    engine = FakeTorrentEngine(streams={_MAGNET_720: _LAN})
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=_SHOW_URL).respond(
            200, text=_fixture("series_show_tt8740758.json")
        )
        async with httpx.AsyncClient() as http:
            p = YtsProvider(engine=engine)
            info = await p.content("tt8740758", http)   # PlaybackInfo path (bare id)
            assert info.id == "yts:tt8740758"
            await p.stream(_S1E1, None, http)                # stream path
            await p.stream(_S1E1, None, http)                # vtt re-resolution
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_playback_survives_popcorn_outage_within_ttl(monkeypatch):
    """Acceptance: once fetched, the in-TTL chain must NOT re-hit the
    upstream — an outage after the first fetch cannot break playback."""
    _configured(monkeypatch)
    engine = FakeTorrentEngine(streams={_MAGNET_720: _LAN})
    p = YtsProvider(engine=engine)
    async with httpx.AsyncClient() as http:
        with respx.mock:
            respx.get(url=_SHOW_URL).respond(
                200, text=_fixture("series_show_tt8740758.json")
            )
            info = await p.content("tt8740758", http)
            assert info.id == "yts:tt8740758"
        # Upstream now DEAD (no mock) — cached show still serves playback.
        resp = await p.stream(_S1E1, None, http)
        assert resp.url == _LAN.url


@pytest.mark.asyncio
async def test_show_cache_expired_entry_refetches(monkeypatch):
    """Past the TTL the cache must not serve stale data forever: the
    next call refetches (verify via a direct TTL manipulation)."""
    _configured(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url=_SHOW_URL).respond(
            200, text=_fixture("series_show_tt8740758.json")
        )
        async with httpx.AsyncClient() as http:
            p = YtsProvider(engine=FakeTorrentEngine())
            await p.content("tt8740758", http)
            ts, show = p._show_cache["tt8740758"]
            p._show_cache["tt8740758"] = (ts - 301.0, show)  # age past TTL
            await p.content("tt8740758", http)
    assert route.call_count == 2
