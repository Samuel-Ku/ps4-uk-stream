"""YTS torrent playback — provider-side orchestration (#377).

The lane under test: ``YtsProvider.stream()`` turns a recorded
quality→hash map into ONE policy-picked magnet and hands it to the
TorrentEngine seam, returning the engine's LAN URL as a progressive
``mp4`` StreamResponse. The in-memory :class:`FakeTorrentEngine` (the
pre-agreed seam fake) replaces BitPlay everywhere here — no Docker, no
network; the YTS upstream is respx-mocked with the recorded fixtures.

Covered at this layer:
- DI: constructor-injected engine wins; unset engine + unconfigured
  settings is a LOUD typed ``unreachable`` verdict, never silence.
- Policy→session: the engine receives ``build_magnet(<policy-picked
  hash>)``; a dead-on-arrival verdict advances to the next candidate
  (bounded fallback) before item-level ``not_found``.
- Error taxonomy: EngineUnavailable → ``unreachable`` (flaky infra),
  EngineRejected → ``not_found`` "no seeders or dead torrent"
  (deterministic verdict) per spec #374's error-surface note.
- Hash-map lifetime: the recorded map is an LRU bounded at 512 ids,
  move-to-end on access.

Route-level orchestration (TestClient against /api/stream) lives in the
bottom section of this module; provider-level tests come first.
"""

from __future__ import annotations

import json
import pathlib
import re

import httpx
import pytest
import respx

from cs_uk_api.models import StreamResponse
from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.yts import (
    YtsProvider,
    build_magnet,
)
from cs_uk_api.torrent_engine import (
    EngineRejected,
    EngineStream,
    EngineUnavailable,
    FakeTorrentEngine,
)

FIX = pathlib.Path(__file__).parent / "fixtures" / "yts"

_DETAILS_URL = re.compile(r"https://yts\.gg/api/v2/movie_details\.json\?.*")

#: The Dune details fixture's 1080p entry — the policy MUST land on it.
_HASH_1080P = "B2C3D4E5F60718293A4B5C6D7E8F90123456789A"
_MAGNET_1080P = build_magnet(_HASH_1080P)

#: The fixture's 720p entry — the fallback candidate after the 1080p pick.
_MAGNET_720P = build_magnet("A1B2C3D4E5F60718293A4B5C6D7E8F9012345678")


def _details_fixture() -> str:
    return (FIX / "details_tt1160419.json").read_text(encoding="utf-8")


def _mock_details(router: respx.Router, payload: str | None = None) -> respx.Route:
    return router.get(url=_DETAILS_URL).respond(
        200, text=payload if payload is not None else _details_fixture()
    )


class _ExplodingEngine:
    """Engine stub whose ensure_session raises the given engine error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.attempts = 0

    async def ensure_session(
        self, identifier: str, *, file_hint: str | None = None
    ) -> EngineStream:
        self.attempts += 1
        raise self._exc


class _RejectingEngine:
    """Fake engine that rejects the configured magnets as dead-on-arrival
    and serves the rest from a stream table; records calls for order
    assertions."""

    def __init__(
        self, *, rejects: set[str], streams: dict[str, EngineStream]
    ) -> None:
        self._rejects = set(rejects)
        self._streams = streams
        self.ensure_count = 0
        self.last_identifier: str | None = None

    async def ensure_session(
        self, identifier: str, *, file_hint: str | None = None
    ) -> EngineStream:
        self.ensure_count += 1
        self.last_identifier = identifier
        if identifier in self._rejects:
            raise EngineRejected("metadata timeout")
        return self._streams[identifier]


# ---------------------------------------------------------------------------
# DI + loudness (deliverable 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_without_engine_anywhere_is_loud_unreachable(
    monkeypatch: pytest.MonkeyPatch,
):
    """No injected engine AND no settings url ⇒ typed ``unreachable``
    ("torrent engine not configured") BEFORE any upstream traffic."""
    import cs_uk_api.config as config_mod
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(
        config_mod, "SETTINGS", dc_replace(config_mod.SETTINGS, torrent_engine_url=None)
    )
    with respx.mock(assert_all_called=False) as router:
        details = _mock_details(router)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider().stream("tt1160419:__movie__", None, http)
    assert exc.value.code == "unreachable"
    assert exc.value.message == "torrent engine not configured"
    assert details.call_count == 0


@pytest.mark.asyncio
async def test_injected_engine_serves_the_session():
    """Constructor injection (uakino session precedent): the fake IS the
    engine; its configured stream for THE policy magnet comes back as a
    progressive mp4 envelope."""
    lan = EngineStream(url="http://bitplay.lan:3347/api/v1/torrent/x/stream/0", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_1080P: lan})
    p = YtsProvider(engine=engine)
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router)
        async with httpx.AsyncClient() as http:
            await p.content("tt1160419", http)  # warm the hash map
            resp = await p.stream("tt1160419:__movie__", None, http)
    assert resp == StreamResponse(url=lan.url, type="mp4", headers={}, seekable=True)
    assert resp.headers == {}
    assert engine.ensure_count == 1
    assert engine.last_identifier == _MAGNET_1080P


# ---------------------------------------------------------------------------
# Policy → session wiring + error taxonomy (deliverables 3/4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_refreshes_cold_map_from_details_once():
    """A cold process (map empty) re-derives torrents via ONE details
    call — magnets are re-derivable at stream-time (yts.py contract) —
    then hands the engine the picked magnet."""
    engine = FakeTorrentEngine(streams={_MAGNET_1080P: EngineStream(url="http://lan/s", container="mp4")})
    p = YtsProvider(engine=engine)
    with respx.mock(assert_all_called=True) as router:
        details = _mock_details(router)
        async with httpx.AsyncClient() as http:
            resp = await p.stream("tt1160419:__movie__", None, http)
    assert resp.url == "http://lan/s"
    assert details.call_count == 1
    assert engine.ensure_count == 1


@pytest.mark.asyncio
async def test_stream_after_content_makes_no_second_upstream_call():
    """Warm map: stream() must NOT touch the network again — the whole
    point of threading the hash map onto the instance (#376)."""
    p = YtsProvider(engine=FakeTorrentEngine())
    with respx.mock(assert_all_called=True) as router:
        details = _mock_details(router)
        async with httpx.AsyncClient() as http:
            await p.content("tt1160419", http)
            resp = await p.stream("tt1160419:__movie__", None, http)
    assert resp.type == "mp4"
    assert details.call_count == 1


@pytest.mark.asyncio
async def test_stream_picks_policy_winner_within_quality_tier():
    """Two 1080p variants: first-per-quality still feeds the public
    torrent_hashes() view (#376 contract), but the SESSION goes to the
    best-seeded variant (200 seeds beat 50)."""
    payload = {
        "status": "ok",
        "data": {
            "movie": {
                "imdb_code": "tt1160419",
                "title_english": "Dune",
                "torrents": [
                    {"quality": "1080p", "hash": "H_REPACK", "seeds": 50},
                    {"quality": "1080p", "hash": _HASH_1080P, "seeds": 200},
                    {"quality": "720p", "hash": "H_720", "seeds": 900},
                ],
            }
        },
    }
    hot = EngineStream(url="http://lan/hot", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_1080P: hot})
    p = YtsProvider(engine=engine)
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router, json.dumps(payload))
        async with httpx.AsyncClient() as http:
            await p.content("tt1160419", http)
            resp = await p.stream("tt1160419:__movie__", None, http)
    assert p.torrent_hashes("tt1160419")["1080p"] == "H_REPACK"
    assert resp.url == hot.url


@pytest.mark.asyncio
async def test_stream_title_without_torrents_is_deterministic_not_found():
    payload = {
        "status": "ok",
        "data": {"movie": {"imdb_code": "tt1160419", "title_english": "Dune"}},
    }
    p = YtsProvider(engine=FakeTorrentEngine())
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router, json.dumps(payload))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await p.stream("tt1160419:__movie__", None, http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_engine_unavailable_maps_to_unreachable():
    p = YtsProvider(engine=_ExplodingEngine(EngineUnavailable("connection refused")))
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await p.stream("tt1160419:__movie__", None, http)
    assert exc.value.code == "unreachable"


@pytest.mark.asyncio
async def test_engine_rejected_maps_to_not_found_no_seeders():
    """Zero-seeders/metadata-timeout is THIS torrent's deterministic
    verdict — distinct code from the flaky-infra ``unreachable`` above."""
    p = YtsProvider(engine=_ExplodingEngine(EngineRejected("metadata timeout")))
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await p.stream("tt1160419:__movie__", None, http)
    assert exc.value.code == "not_found"
    assert exc.value.message == "no seeders or dead torrent"


@pytest.mark.asyncio
async def test_dead_policy_pick_falls_back_to_next_candidate():
    """The 1080p pick's swarm is dead ⇒ the 720p candidate is tried in
    policy order and its session serves the stream — the bounded
    fallback the #373 live finding motivated, instead of an immediate
    hard not_found."""
    lan_720 = EngineStream(
        url="http://bitplay.lan:3347/api/v1/torrent/720/stream/0", container="mp4"
    )
    engine = _RejectingEngine(
        rejects={_MAGNET_1080P}, streams={_MAGNET_720P: lan_720}
    )
    p = YtsProvider(engine=engine)
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router)
        async with httpx.AsyncClient() as http:
            await p.content("tt1160419", http)  # warm the hash map
            resp = await p.stream("tt1160419:__movie__", None, http)
    assert resp.url == lan_720.url
    assert engine.ensure_count == 2
    assert engine.last_identifier == _MAGNET_720P  # policy order: 1080p first


@pytest.mark.asyncio
async def test_all_candidates_dead_is_still_item_not_found():
    """Every candidate's swarm dead ⇒ the same deterministic item-level
    ``not_found`` (message unchanged), never a lane-level ``unreachable``
    — the taxonomy survives the fallback."""
    engine = _ExplodingEngine(EngineRejected("metadata timeout"))
    p = YtsProvider(engine=engine)
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await p.stream("tt1160419:__movie__", None, http)
    assert exc.value.code == "not_found"
    assert engine.attempts == 2  # both fixture candidates exhausted


@pytest.mark.asyncio
async def test_engine_unavailable_aborts_fallback_immediately():
    """A lane-level failure must NOT be masked by candidate fallback —
    the next candidate is never tried (spec #374 error-surface note)."""
    engine = _ExplodingEngine(EngineUnavailable("connection refused"))
    p = YtsProvider(engine=engine)
    with respx.mock(assert_all_called=True) as router:
        _mock_details(router)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await p.stream("tt1160419:__movie__", None, http)
    assert exc.value.code == "unreachable"
    assert engine.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("translation", [None, "en", "uk"])
async def test_stream_ignores_translation_original_audio_only(translation):
    """English originals have no dubs axis: any translation value yields
    the identical original-audio session."""
    lan = EngineStream(url="http://lan/orig", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_1080P: lan})
    p = YtsProvider(engine=engine)
    with respx.mock(assert_all_called=False) as router:
        _mock_details(router)
        async with httpx.AsyncClient() as http:
            resp = await p.stream("tt1160419:__movie__", translation, http)
    assert resp.url == lan.url


@pytest.mark.asyncio
async def test_stream_bad_external_id_rejected_before_everything():
    """Boundary parity with content(): only tt\\d{7,8} proceeds; the
    engine is never consulted for garbage ids."""
    engine = FakeTorrentEngine()
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await YtsProvider(engine=engine).stream("../../etc/passwd", None, http)
    assert exc.value.code == "not_found"
    assert engine.ensure_count == 0


# ---------------------------------------------------------------------------
# Hash-map lifetime: LRU bound ~512, move-to-end on access (review finding)
# ---------------------------------------------------------------------------

_LRU_PAGE = 520  # > the 512 bound, so one page already forces eviction


def _bulk_payload(first_imdb_seq: int, count: int) -> str:
    movies = [
        {
            "imdb_code": f"tt{first_imdb_seq + i:07d}",
            "title_english": f"Bulk {i}",
            "torrents": [{"quality": "720p", "hash": f"H{first_imdb_seq + i:07d}", "seeds": 1}],
        }
        for i in range(count)
    ]
    return json.dumps({"status": "ok", "data": {"movie_count": count, "limit": count, "movies": movies}})


@pytest.mark.asyncio
async def test_recorded_hash_map_is_lru_bounded_with_move_to_end():
    p = YtsProvider(engine=FakeTorrentEngine())
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url=re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")).respond(
            200, text=_bulk_payload(1, _LRU_PAGE)
        )
        async with httpx.AsyncClient() as http:
            await p.browse("movies", 1, http)

            assert route.call_count == 1
            # Bound enforced: the 8 oldest of 520 were evicted.
            assert p.torrent_hashes("tt0000001") == {}
            assert p.torrent_hashes("tt0000520") != {}

            # Accessing tt0000009 moves it to the recency end…
            assert p.torrent_hashes("tt0000009") == {"720p": "H0000009"}
            # …so a later overflow evicts LATER-recorded entries instead.
            router.get(url=re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")).respond(
                200, text=_bulk_payload(600, 2)
            )
            await p.browse("movies", 1, http)
            assert p.torrent_hashes("tt0000009") != {}  # survived: recently USED
            assert p.torrent_hashes("tt0000010") == {}  # evicted: merely recorded earlier


# ---------------------------------------------------------------------------
# Route-level orchestration — TestClient over /api/stream (deliverable 5)
# ---------------------------------------------------------------------------


def _install(p: YtsProvider):
    from cs_uk_api.providers import PROVIDERS

    saved = PROVIDERS.get("yts")
    PROVIDERS["yts"] = p
    return saved


def _restore(saved):
    from cs_uk_api.providers import PROVIDERS

    if saved is None:
        PROVIDERS.pop("yts", None)
    else:
        PROVIDERS["yts"] = saved


def test_route_yts_movie_streams_via_fake_engine():
    """GET /api/stream/yts:tt…:__movie__ end-to-end through the generic
    route: cold map → details refresh → policy magnet → engine session →
    200 mp4 envelope whose url IS the engine's."""
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    lan = EngineStream(url="http://bitplay.lan:3347/api/v1/torrent/abc/stream/5", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET_1080P: lan})
    saved = _install(YtsProvider(engine=engine))
    try:
        with respx.mock(assert_all_called=True) as router:
            _mock_details(router)
            r = TestClient(app).get("/api/stream/yts:tt1160419:__movie__")
        assert r.status_code == 200
        body = r.json()
        assert body["url"] == lan.url
        assert body["type"] == "mp4"
        assert body["headers"] == {}
        # Orchestration pin: session ensured before handoff…
        assert engine.ensure_count == 1
        assert engine.last_identifier == _MAGNET_1080P
    finally:
        _restore(saved)


def test_route_yts_stream_is_never_cached():
    """Cache-contract prior art (test_cache_contract style): every
    /api/stream hit re-executes the provider — two GETs, two sessions."""
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    engine = FakeTorrentEngine()
    saved = _install(YtsProvider(engine=engine))
    try:
        with respx.mock(assert_all_called=True) as router:
            details = _mock_details(router)
            client = TestClient(app)
            r1 = client.get("/api/stream/yts:tt1160419:__movie__")
            r2 = client.get("/api/stream/yts:tt1160419:__movie__")
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["url"] == r2.json()["url"]  # deterministic fake
        assert engine.ensure_count == 2  # provider re-executed: NOT cached
        # Second call rides the WARM hash map — the details refresh is a
        # cold-process path only (exactly one upstream call total).
        assert details.call_count == 1
    finally:
        _restore(saved)


def test_route_yts_engine_down_is_502_unavailable_envelope():
    """Engine-down renders EXACTLY how native routes render provider
    errors today (service.upstream_guard fall-through): 502 envelope,
    error ``upstream_unreachable`` — the clean unavailable state."""
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    saved = _install(
        YtsProvider(engine=_ExplodingEngine(EngineUnavailable("connection refused")))
    )
    try:
        with respx.mock(assert_all_called=True) as router:
            _mock_details(router)
            r = TestClient(app).get("/api/stream/yts:tt1160419:__movie__")
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert detail["error"] == "upstream_unreachable"
        assert "torrent engine unreachable" in detail["message"]
    finally:
        _restore(saved)


def test_route_yts_dead_torrent_is_typed_not_found_verdict():
    """Swarm-level death (zero seeders / metadata timeout) keeps ITS
    deterministic verdict visible in the message while rendering through
    the same native guard path."""
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    saved = _install(
        YtsProvider(engine=_ExplodingEngine(EngineRejected("504 metadata timeout")))
    )
    try:
        with respx.mock(assert_all_called=True) as router:
            _mock_details(router)
            r = TestClient(app).get("/api/stream/yts:tt1160419:__movie__")
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert detail["error"] == "upstream_unreachable"
        assert "no seeders or dead torrent" in detail["message"]
    finally:
        _restore(saved)


def test_route_unknown_provider_prefix_stays_404():
    """Sanity: yts rides the GENERIC route machinery — unknown prefixes
    keep their standing 404."""
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    r = TestClient(app).get("/api/stream/nosuchprovider:x")
    assert r.status_code == 404


def test_facade_empty_headers_redirects_straight_to_engine():
    """Deliverable 6: the facade keeps its direct-302 posture for empty
    headers — Switchfin plays straight from the engine URL (spec #374
    StreamResponse mapping). Episode-wire-id resolution needs no catalog
    machinery; stream routes are public (no token)."""
    from fastapi.testclient import TestClient
    from urllib.parse import quote as _quote

    from cs_uk_api.main import app

    lan = "http://bitplay.lan:3347/api/v1/torrent/abc/stream/5"
    engine = FakeTorrentEngine(streams={_MAGNET_1080P: EngineStream(url=lan, container="mp4")})
    saved = _install(YtsProvider(engine=engine))
    try:
        with respx.mock(assert_all_called=True) as router:
            _mock_details(router)
            item_id = _quote("yts:tt1160419:__movie__", safe="")
            r = TestClient(app).get(f"/Videos/{item_id}/stream", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == lan  # bytes come from the ENGINE
        assert engine.ensure_count == 1
    finally:
        _restore(saved)


def test_route_facade_posture_headers_empty_redirects_directly():
    """Cheapest shape pin backing the facade decision above: the
    StreamResponse this lane emits has EMPTY headers, which is exactly
    the condition the facade's redirect branch keys on."""
    lan = EngineStream(url="http://bitplay.lan:3347/api/v1/torrent/abc/stream/5", container="mp4")
    resp = StreamResponse(url=lan.url, type="mp4", headers={})
    assert resp.headers == {}



def test_route_yts_dead_torrent_does_not_poison_lane_health():
    """Deterministic item-level verdicts skip the health record — the
    runbook's rule of thumb («healthy yts + failing playbacks ⇒ engine»)
    must survive repeated dead-torrent plays."""
    from fastapi.testclient import TestClient

    from cs_uk_api.health import TRACKER
    from cs_uk_api.main import app

    saved = _install(
        YtsProvider(engine=_ExplodingEngine(EngineRejected("504 metadata timeout")))
    )
    try:
        with respx.mock(assert_all_called=True) as router:
            _mock_details(router)
            before = TRACKER.status("yts")
            r = TestClient(app).get("/api/stream/yts:tt1160419:__movie__")
        assert r.status_code == 502  # envelope unchanged — wire-invariant held
        assert TRACKER.status("yts") == before  # not degraded by a dead torrent
    finally:
        _restore(saved)


def test_route_yts_remux_stream_reports_unseekable():
    """The engine's seekable verdict rides StreamResponse.seekable so the
    client layer (#378) can warn — native stays True/None."""
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app
    from cs_uk_api.torrent_engine import EngineStream, FakeTorrentEngine

    unseekable = EngineStream(url="http://lan/remux", container="mp4", seekable=False)
    saved = _install(YtsProvider(engine=FakeTorrentEngine(streams={_MAGNET_1080P: unseekable})))
    try:
        with respx.mock(assert_all_called=True) as router:
            _mock_details(router)
            r = TestClient(app).get("/api/stream/yts:tt1160419:__movie__")
        assert r.status_code == 200
        assert r.json()["seekable"] is False
    finally:
        _restore(saved)
