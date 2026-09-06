"""Behavior-verification suite — the adversarial probes made permanent.

The refactor passes (#387–#390) were verified with throwaway headless
probes through the real app. This module keeps the behaviors those
probes pinned that the per-feature suites do NOT already cover, so CI
holds the line as the code evolves (everything the probe pass merely
re-verified — byte-proxy Range echo, segment SSRF unit, seasons rail,
resume round-trip, dub memory, RFC 5987 naming — already lives in its
feature suite and is deliberately not duplicated here):

``integration``
    S — the English viewer arc on the facade: the ONE-fetch session
    (PlaybackInfo → stream → VTT = a single Popcorn fetch), the ≥95%
    finish transition (the episode leaves the resume rail AND drains
    NextUp in one transaction), and the churned-title download name on
    the English lane.
    A — the flat-registration invariants (#389/#390): no duplicate
    (path, method) pairs across the facade router, the deliberate
    public-no-token posture of the delivery byte routes vs the gated
    Sessions reports, and OpenAPI generation over the moved routes.
``e2e``
    E — the REAL ``BitPlayClient`` (built from settings by the lazy
    singleton) against a live local TCP origin speaking the BitPlay
    wire, end to end through the facade: show fixture → PlaybackInfo
    (engine truth + #378 subtitle surface) → stream 302 → real bytes,
    VTT route → engine srt→vtt.
    E — the session-cost contract fused with the health lane: item-level
    verdicts (``not_found``) do NOT move lane health (ADR-0002: 404
    codes are client-side semantics); a transport failure flips the
    provider DOWN; successes self-heal the window — all on ONE fetch.

Markers: the two live-socket tests carry ``e2e`` — they are hermetic
(loopback TCP, <1s) and run in the DEFAULT suite so CI pins them;
constrained environments can deselect with ``-m "not e2e"``.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import quote

import httpx
import pytest
import respx
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from cs_uk_api._catalog_state import blocklist_cache, content_cache, home_cache, sources_cache
from cs_uk_api.config import SETTINGS
from cs_uk_api.health import TRACKER
from cs_uk_api.models import SearchResult
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.providers.yts import YtsProvider
from cs_uk_api.torrent_engine import (
    EngineStream,
    EngineUnavailable,
    FakeTorrentEngine,
    reset_engine,
)
from cs_uk_api.wire_identity import episode_wire_id

TOKEN = SETTINGS.jellyfin_token
FIX = pathlib.Path(__file__).parent / "fixtures" / "yts"

router_mod = importlib.import_module("cs_uk_api.jellyfin.router")
delivery_mod = importlib.import_module("cs_uk_api.jellyfin.delivery")


class _Silent(BaseProvider):
    """A registered-but-silent Ukrainian-lane stand-in: nothing to fetch."""

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    allowed_hosts = frozenset({"p1.example"})

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def content(self, external_id: str, http: Any) -> Any:  # pragma: no cover
        raise AssertionError("not used in this module")

    async def stream(  # pragma: no cover
        self, content_id: str, translation: str | None, http: Any
    ) -> Any:
        raise AssertionError("not used in this module")


_POPCORN = "http://popcorn.lan:9000"
_SHOW_URL = re.compile(rf"{re.escape(_POPCORN)}/show/tt8740758")
_LIST_URL = re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")
# The SEASON magnet: stream() plays the season's policy winner (720p
# dominates quality; the most-seeded 720p across the season's episodes
# wins) — both S1E1 and S1E2 ride this ONE engine session.
_MAGNET_SEASON = json.loads((FIX / "series_show_tt8740758.json").read_text(encoding="utf-8"))[
    "episodes"
][0]["torrents"]["720p"]["url"]
_LAN = "http://bitplay.lan:3347/api/v1/torrent/s01/stream/2"
_S1E1 = episode_wire_id("yts", "tt8740758", 1, 1)
_S1E2 = episode_wire_id("yts", "tt8740758", 1, 2)
_SHOW = (FIX / "series_show_tt8740758.json").read_text(encoding="utf-8")


def _torrentless_show() -> str:
    """The series fixture with every torrent stripped — a season the
    provider can resolve but not play (deterministic ``not_found``)."""
    payload = json.loads(_SHOW)
    for ep in payload["episodes"]:
        ep["torrents"] = {}
    return json.dumps(payload)


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the popcorn knob at the fake host — BEFORE any YtsProvider
    construction (the base is snapshotted at construction time)."""
    from dataclasses import replace as dc_replace

    import cs_uk_api.config as config_mod

    monkeypatch.setattr(
        config_mod, "SETTINGS", dc_replace(config_mod.SETTINGS, popcorn_base_url=_POPCORN)
    )


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """The yts-driven sections need the registry reduced to the stub plus
    the real YTS provider — same isolation the other facade suites use."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
        cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
            cache.clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def _seed_series(monkeypatch: pytest.MonkeyPatch, *, subtitle: bool = False) -> None:
    """Registry = silent stub + real YTS over the fake engine (the
    popcorn base must be configured BEFORE construction)."""
    _configure(monkeypatch)
    stream = EngineStream(
        url=_LAN, container="mp4", subtitle_url=f"{_LAN}.vtt" if subtitle else None
    )
    PROVIDERS["p1"] = _Silent()
    PROVIDERS["yts"] = YtsProvider(engine=FakeTorrentEngine(streams={_MAGNET_SEASON: stream}))


def _warm_home(client: TestClient) -> str:
    """Run /api/home once (the real client order) and return the series'
    group key."""
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_LIST_URL).respond(
            200,
            text=json.dumps(
                {"status": "ok", "data": {"movie_count": 0, "limit": 50, "movies": []}}
            ),
        )
        router.get(url=re.compile(rf"{re.escape(_POPCORN)}/shows/\d+\?.*")).respond(
            200, text=(FIX / "series_page2_last.json").read_text(encoding="utf-8")
        )
        r = client.get("/api/home")
    assert r.status_code == 200, r.text
    home = cast("dict[str, Any]", r.json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Chernobyl":
                return cast(str, item["group_key"])
    raise AssertionError("the English series card never reached the home snapshot")


# ===========================================================================
# S — the viewer arc (integration): the probe-only behaviors
# ===========================================================================


def test_playback_session_costs_one_popcorn_fetch(client: TestClient, monkeypatch) -> None:
    """PlaybackInfo → stream → VTT for one episode = exactly ONE upstream
    show fetch: the session-slice TTL cache serving the whole viewing
    session on the FACADE surface (the provider-level pin rides
    test_yts_series; this one dies if the facade grows a second
    resolution layer)."""
    _seed_series(monkeypatch, subtitle=True)
    _warm_home(client)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url=_SHOW_URL).respond(200, text=_SHOW)
        info = client.post(
            f"/Items/{quote(_S1E1, safe='')}/PlaybackInfo",
            params={"userId": "u"},
            headers={"X-Emby-Token": TOKEN},
            json={},
        )
        assert info.status_code == 200
        stream = client.get(f"/Videos/{quote(_S1E1, safe='')}/stream", follow_redirects=False)
        assert stream.status_code == 302
        assert stream.headers["location"] == _LAN
        vtt = client.get(f"/Videos/{quote(_S1E1, safe='')}/vtt", follow_redirects=False)
        assert vtt.status_code == 302
        assert vtt.headers["location"] == f"{_LAN}.vtt"
    assert route.call_count == 1


def test_finish_transition_empties_resume_and_nextup(client: TestClient, monkeypatch) -> None:
    """Watching to the end (≥95% of runtime) removes the episode from the
    resume rail AND drains NextUp in the same transaction — the finished
    state, not merely a hidden row."""
    _seed_series(monkeypatch)
    _warm_home(client)
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(200, text=_SHOW)
        # Mid-play: both shelves point at the episode family.
        client.post(
            "/Sessions/Playing/Stopped",
            headers={"X-Emby-Token": TOKEN},
            json={
                "ItemId": _S1E1,
                "PositionTicks": 1_800_000_000,
                "RunTimeTicks": 3_600_000_000,
            },
        )
        nextup_mid = client.get("/Shows/NextUp", headers={"X-Emby-Token": TOKEN})
        assert nextup_mid.status_code == 200
        assert any(
            e["Id"] == _S1E2 for e in cast("list[dict[str, Any]]", nextup_mid.json()["Items"])
        )
        # Finish the IN-PROGRESS episode at 96.5% of its runtime —
        # the watched episode leaves the resume shelf, and with no
        # later episode reported, NextUp has nothing to offer.
        client.post(
            "/Sessions/Playing/Stopped",
            headers={"X-Emby-Token": TOKEN},
            json={
                "ItemId": _S1E1,
                "PositionTicks": 3_474_000_000,
                "RunTimeTicks": 3_600_000_000,
            },
        )
        resume = client.get("/Users/x/Items/Resume", headers={"X-Emby-Token": TOKEN})
        nextup = client.get("/Shows/NextUp", headers={"X-Emby-Token": TOKEN})
    assert resume.status_code == 200
    assert nextup.status_code == 200
    assert all(e["Id"] != _S1E1 for e in cast("list[dict[str, Any]]", resume.json()["Items"]))
    assert nextup.json()["Items"] == []


def test_download_names_file_from_churned_title(client: TestClient, monkeypatch) -> None:
    """/Items/{id}/Download on the ENGLISH lane names the file after the
    (churned) series title — the detail-open warm path a real client
    takes first — riding the RFC 5987 Cyrillic form."""
    _seed_series(monkeypatch)
    gk = _warm_home(client)
    churned = json.loads(_SHOW)
    churned["title"] = "Чорнобиль (оновлено)"
    for ep in churned["episodes"]:
        ep["title"] = f"Серія {ep['episode']} (нова)"  # Cyrillic ⇒ RFC 5987 branch
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(200, text=json.dumps(churned))
        # The default-voice download proxies the stream's LAN url (no
        # provider headers ⇒ no 302) — serve the engine hop too.
        router.get(url=_LAN).mock(
            return_value=httpx.Response(
                200, content=b"download-bytes", headers={"Content-Type": "video/mp4"}
            )
        )
        detail = client.get(f"/Items/{quote(gk, safe='')}", headers={"X-Emby-Token": TOKEN})
        assert detail.status_code == 200
        download = client.get(
            f"/Items/{quote(_S1E1, safe='')}/Download", headers={"X-Emby-Token": TOKEN}
        )
        assert download.status_code == 200
        assert download.content == b"download-bytes"
        disposition = download.headers.get("content-disposition", "")
    assert disposition.startswith("attachment; filename=")
    assert "filename*=UTF-8''" in disposition
    from urllib.parse import unquote

    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == (
        "Серія_1_(нова).mp4"  # safe_filename underscores spaces (by design)
    )


# ===========================================================================
# A — registration invariants (integration)
# ===========================================================================


def test_facade_router_has_no_duplicate_path_method_pairs() -> None:
    """The flat-registration hazard (#389/#390): delivery.register() plus
    the decorator routes must never produce two routes for one
    (path, method) pair — the one real corruption risk of moving routes
    between modules."""
    seen: dict[tuple[str, str], str] = {}
    for route in router_mod.router.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                key = (route.path, method)
                assert key not in seen, (
                    f"duplicate route {method} {route.path}: "
                    f"{seen[key]} and {route.endpoint.__module__}"
                )
                seen[key] = route.endpoint.__module__


_DELIVERY_PATHS = (
    "/Videos/{item_id:path}/stream",
    "/Videos/{item_id:path}/vtt",
    "/Stream/{item_id:path}/vtt",
    "/Items/{item_id:path}/Download",
    "/Videos/{item_id:path}/segment",
)


def test_delivery_byte_routes_public_and_reports_gated() -> None:
    """The deliberate token posture, pinned on the route table: the moved
    delivery byte routes carry NO auth dependency (Switchfin's player
    fetches them without a session), while Sessions report ingestion
    stays behind require_token."""
    from cs_uk_api.jellyfin.auth import require_token

    found = 0
    for route in router_mod.router.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in _DELIVERY_PATHS:
            found += 1
            assert route.dependencies == [], f"{route.path} must stay public"
        elif route.path.startswith("/Sessions/"):
            assert any(
                getattr(d, "dependency", None) is require_token for d in route.dependencies
            ), f"{route.path} must stay behind require_token"
    assert found == len(_DELIVERY_PATHS), "a delivery route went missing"


def test_openapi_still_covers_the_moved_routes() -> None:
    """The whole facade surface keeps generating an OpenAPI document, and
    every moved delivery route is present in it (a module move must not
    make routes client-invisible)."""
    from cs_uk_api import main as main_mod

    paths = main_mod.app.openapi()["paths"]
    for path in (
        "/Videos/{item_id}/stream",
        "/Videos/{item_id}/vtt",
        "/Stream/{item_id}/vtt",
        "/Items/{item_id}/Download",
        "/Videos/{item_id}/segment",
    ):
        assert path in paths, f"{path} missing from OpenAPI"


# ===========================================================================
# E — live BitPlay socket (e2e): the real adapter over real TCP
# ===========================================================================


class _OriginWire(BaseHTTPRequestHandler):
    """One local TCP origin speaking BOTH upstream wires the live path
    touches: the BitPlay engine API (``/api/v1/...``) and the Popcorn
    shows API (``/show/{imdb}``) — so the e2e drives the real facade, the
    real provider HTTP path and the real BitPlayClient with NO respx in
    the loop (respx pass-through is avoided deliberately)."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        self.server.requests.append(("POST", self.path, body))  # type: ignore[attr-defined]
        self._json(200, {"sessionId": "s01"})

    def do_GET(self) -> None:
        self.server.requests.append(("GET", self.path))  # type: ignore[attr-defined]
        if self.path.startswith("/api/v1/torrent/s01/stream/2"):
            self._raw(b"NATIVE_MP4_BYTES", "video/mp4")
        elif self.path.startswith("/api/v1/torrent/s01/stream/1"):
            self._raw(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nline\n", "text/vtt")
        elif self.path.startswith("/api/v1/torrent/s01"):
            self._json(
                200,
                [{"index": 1, "name": "show.en.srt"}, {"index": 2, "name": "show.mp4"}],
            )
        elif self.path.startswith("/show/"):
            self._json(200, json.loads(_SHOW))
        else:
            self._json(404, {"error": "no route"})

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # No keep-alive: the shared get_client() pool must not hold real
        # sockets past this test — a later test's event loop would try to
        # close them (RuntimeError: Event loop is closed).
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # keep the test output clean
        pass


@pytest.fixture()
def origin_server() -> Iterator[tuple[str, list[Any]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OriginWire)
    server.daemon_threads = True
    server.requests = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.requests
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.e2e
@pytest.mark.e2e
def test_live_bitplay_socket_end_to_end(
    client: TestClient, monkeypatch, origin_server: tuple[str, list[Any]]
) -> None:
    """The REAL assembly (settings → lazy singleton → BitPlayClient) and
    the REAL provider HTTP path against one live local origin, end to end
    through the facade: show fetch → PlaybackInfo (engine truth + the
    #378 subtitle surface) → stream 302 to the native-mp4 LAN url → real
    bytes; VTT route → the engine's srt→vtt conversion. NO respx — every
    hop is real TCP over loopback."""
    base, requests = origin_server
    from dataclasses import replace as dc_replace

    import cs_uk_api.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "SETTINGS",
        dc_replace(
            config_mod.SETTINGS,
            torrent_engine_url=base,
            popcorn_base_url=base,  # the same origin serves the shows API
        ),
    )
    reset_engine()
    try:
        _run_live_socket(client, base, requests)
    finally:
        reset_engine()


def _run_live_socket(client: TestClient, base: str, requests: list[Any]) -> None:
    """The live conversation: provider registry = silent stub + the real
    YtsProvider over the real engine; every assertion rides real TCP."""
    PROVIDERS["p1"] = _Silent()
    PROVIDERS["yts"] = YtsProvider()  # defers to get_engine() → the real client
    info = client.post(
        f"/Items/{quote(_S1E1, safe='')}/PlaybackInfo",
        params={"userId": "u"},
        headers={"X-Emby-Token": TOKEN},
        json={},
    )
    assert info.status_code == 200
    source = cast("dict[str, Any]", info.json())["MediaSources"][0]
    subs = [s for s in source["MediaStreams"] if s["Type"] == "Subtitle"]
    assert subs and "/vtt" in subs[0]["DeliveryUrl"]

    stream = client.get(f"/Videos/{quote(_S1E1, safe='')}/stream", follow_redirects=False)
    assert stream.status_code == 302
    engine_url = stream.headers["location"]
    assert engine_url == f"{base}/api/v1/torrent/s01/stream/2"

    vtt = client.get(f"/Videos/{quote(_S1E1, safe='')}/vtt", follow_redirects=False)
    assert vtt.status_code == 302
    assert vtt.headers["location"].endswith("/api/v1/torrent/s01/stream/1?format=vtt")

    # The player bytes come over real TCP.
    mp4 = httpx.get(engine_url, timeout=5.0)
    assert mp4.status_code == 200
    assert mp4.content == b"NATIVE_MP4_BYTES"
    assert mp4.headers["content-type"] == "video/mp4"
    vtt_body = httpx.get(vtt.headers["location"], timeout=5.0)
    assert vtt_body.status_code == 200
    assert vtt_body.content.startswith(b"WEBVTT")

    # The wire saw the BitPlay conversation: the popcorn magnet used
    # VERBATIM (the tv.js contract), the file listing served the picks.
    adds = [r for r in requests if r[0] == "POST" and r[1].endswith("/torrent/add")]
    assert adds and adds[0][2]["Magnet"] == _MAGNET_SEASON
    listings = [r for r in requests if r[0] == "GET" and r[1].endswith("/torrent/s01")]
    assert listings, "the file listing never ran"
    shows = [r for r in requests if r[0] == "GET" and r[1].startswith("/show/")]
    assert shows, "the popcorn show fetch never ran"


@pytest.mark.e2e
def test_facade_session_cost_and_health_lane(client: TestClient, monkeypatch) -> None:
    """Fused session-cost + health contract through the facade. Item-level
    verdicts (``not_found`` — a torrent-less season upstream) do NOT move
    lane health per ADR-0002 (404 codes are client-side semantics, not
    upstream health — the #373 error-surface note applied to the facade's
    recording); a transport failure flips the provider DOWN; and a fresh
    session self-heals the window — each phase costs ONE Popcorn fetch."""

    class _RaisingEngine:
        async def ensure_session(
            self, identifier: str, *, file_hint: str | None = None
        ) -> EngineStream:
            raise EngineUnavailable("connection refused")

    _seed_series(monkeypatch)
    _warm_home(client)
    TRACKER.reset()  # the warm browse recorded successes; pin a clean window

    # Phase A — deterministic item-level verdicts never move the needle:
    # torrent-less upstream refuses as not_found ×5, health stays ok.
    with respx.mock(assert_all_called=False) as router:
        show = router.get(url=_SHOW_URL).respond(200, text=_torrentless_show())
        for _ in range(5):
            r = client.get(f"/Videos/{quote(_S1E1, safe='')}/stream", follow_redirects=False)
            assert r.status_code == 404
        assert TRACKER.status("yts") == "ok"  # item verdicts are not lane faults
        assert show.call_count == 1  # the season map is cached; one fetch total

    # Phase B — a genuine transport failure (engine unreachable) is an
    # ENGINE-path fault (spec #394): it flips the yts:engine entry DOWN
    # while the catalog lane stays ok — user story 16 of spec #374 (tell
    # a dead catalog API from a dead engine). Five refusals, still one
    # fetch for the season map.
    PROVIDERS["yts"] = YtsProvider(engine=_RaisingEngine())
    TRACKER.reset()
    with respx.mock(assert_all_called=False) as router:
        show = router.get(url=_SHOW_URL).respond(200, text=_SHOW)
        for _ in range(5):
            r = client.get(f"/Videos/{quote(_S1E1, safe='')}/stream", follow_redirects=False)
            assert r.status_code == 404
        assert TRACKER.status("yts") == "ok"  # the engine fault is not the catalog lane's
        assert TRACKER.status("yts:engine") == "down"  # it is the engine entry's (spec #394)
        assert show.call_count == 1  # one fetch per session generation

    # Phase C — torrents return upstream; the NEXT session (a fresh
    # provider, the cache generation after the outage) self-heals the
    # window: 5 failures + 15 successes → rate 0.25 < 0.4 → ok. One
    # fetch serves the whole healed session.
    PROVIDERS["yts"] = YtsProvider(
        engine=FakeTorrentEngine(
            streams={
                _MAGNET_SEASON: EngineStream(url=_LAN, container="mp4"),
            }
        )
    )
    with respx.mock(assert_all_called=False) as router:
        show = router.get(url=_SHOW_URL).respond(200, text=_SHOW)
        for _ in range(15):
            r = client.get(f"/Videos/{quote(_S1E1, safe='')}/stream", follow_redirects=False)
            assert r.status_code == 302
        assert TRACKER.status("yts") == "ok"
        assert show.call_count == 1  # the healed session still costs one fetch
