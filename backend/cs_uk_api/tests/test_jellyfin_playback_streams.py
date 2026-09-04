"""PlaybackInfo media streams — subtitle delivery (#378, spec #374).

The single-source PlaybackInfo path stops being blind to the file's
content: when the provider's ``StreamResponse`` reports the engine's
VTT subtitle endpoint, the thin MediaSource's ``MediaStreams`` grows
the matching ``Subtitle`` entry, and ``/Stream/{item}/vtt`` (the
``DeliveryUrl`` target) hands the player the VTT bytes via a 302 to the
engine. No ``Audio`` entries: the engine's file listing cannot see
audio streams inside a file, so any pick would be invented and
unselectable (lean-build omission). A classic response (nothing to report) must leave the wire
BYTE-IDENTICAL to the pre-#378 shape — the Ukrainian-lane parity gate is
pinned here against a frozen fixture.

Ukrainian-lane behaviour under test: the SAME movie card as
``test_jellyfin_playback.py`` (a stub provider) answers with exactly the
historical envelope — no Subtitle entry, no Audio entry, no
``DeliveryUrl`` keys anywhere in the bytes.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api._catalog_state import blocklist_cache, content_cache, home_cache, sources_cache
from cs_uk_api.config import SETTINGS
from cs_uk_api.models import ContentResponse, SearchResult, StreamResponse, Translation
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

TOKEN = SETTINGS.jellyfin_token
FIX = pathlib.Path(__file__).parent / "fixtures" / "jellyfin"
_POSTER_MOVIE = "https://cdn.example.test/posters/dune.jpg"


def _dune() -> ContentResponse:
    return ContentResponse(
        id="p1:dune-1",
        form="movie",
        title="Дюна",
        year=2021,
        description="Епічна науково-фантастична стрічка.",
        poster=_POSTER_MOVIE,
        translations=[Translation(id="uk", label="Дубляж")],
    )


class _PlaybackStub(BaseProvider):
    """Same shape as the playback-module stub: canned streams by id."""

    id = "p1"
    name = "P1"
    types = ("movie",)
    newest_section = "page"

    def __init__(self, streams: dict[str, StreamResponse]) -> None:
        self._streams = streams
        self.stream_calls: list[tuple[str, str | None]] = []

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(self, section: str, page: int, http: Any) -> tuple[list[SearchResult], bool]:
        if section == "page":
            return [
                SearchResult(
                    id="p1:dune-1",
                    provider="p1",
                    form="movie",
                    styles=frozenset(),
                    title="Дюна",
                    year=2021,
                    poster=_POSTER_MOVIE,
                    url="https://p1.example/dune-1",
                )
            ], False
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        return _dune().model_copy(deep=True)

    async def stream(self, content_id: str, translation: str | None, http: Any) -> StreamResponse:
        self.stream_calls.append((content_id, translation))
        stream = self._streams.get(content_id)
        if stream is None:
            raise AssertionError(f"no canned stream for {content_id}")
        return stream.model_copy(deep=True)


def _classic() -> StreamResponse:
    return StreamResponse(url="https://cdn.example.test/dune.mp4", type="mp4")


def _torrent_lane() -> StreamResponse:
    return StreamResponse(
        url="http://bitplay.lan:3347/api/v1/torrent/abc/stream/5",
        type="mp4",
        subtitle_url="http://bitplay.lan:3347/api/v1/torrent/abc/stream/7?format=vtt",
    )


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
        cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
            cache.clear()


def _warm_movie_gk(client: TestClient) -> str:
    r = client.get("/api/home")
    assert r.status_code == 200
    home = cast("dict[str, Any]", r.json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                return cast(str, item["group_key"])
    raise AssertionError("seeded movie card missing from home")


def _post(client: TestClient, path: str) -> dict[str, Any]:
    r = client.post(path, params={"userId": "u"}, headers={"X-Emby-Token": TOKEN}, json={})
    assert r.status_code == 200, r.text
    return cast("dict[str, Any]", r.json())


# --------------------------------------------------------------- enriched


def test_playback_info_enriches_when_session_reports_tracks(client: TestClient) -> None:
    """A torrent-lane StreamResponse grows the wire: Video + Subtitle
    (DeliveryUrl = the facade VTT proxy). No Audio entries — nothing
    the engine listing shows is selectable."""
    stub = _PlaybackStub(streams={"dune-1": _torrent_lane()})
    PROVIDERS["p1"] = stub
    gk = _warm_movie_gk(client)

    body = _post(client, f"/Items/{gk}/PlaybackInfo")
    assert len(body["MediaSources"]) == 1
    source = cast("dict[str, Any]", body["MediaSources"][0])
    assert source["Container"] == "mp4"
    assert source["MediaStreams"] == [
        {"Type": "Video"},
        {"Type": "Subtitle", "DeliveryUrl": f"/Stream/{gk}/vtt"},
    ]


def test_playback_info_omits_cleanly_when_session_bare(client: TestClient) -> None:
    """A session with no srt must carry ONLY the classic
    ``[{Type: Video}]`` stream — the omission is clean."""
    stub = _PlaybackStub(streams={"dune-1": _classic()})
    PROVIDERS["p1"] = stub
    gk = _warm_movie_gk(client)

    body = _post(client, f"/Items/{gk}/PlaybackInfo")
    source = cast("dict[str, Any]", body["MediaSources"][0])
    assert source["MediaStreams"] == [{"Type": "Video"}]
    assert "subtitle_url" not in source


def test_vtt_route_302s_to_engine_and_404s_without_subtitle(client: TestClient) -> None:
    """/Stream/{item}/vtt (the DeliveryUrl target) 302s to the engine's
    ``?format=vtt`` endpoint; a session without one 404s — and so does a
    classic lane item (its stream has no subtitle_url)."""
    stub = _PlaybackStub(
        streams={"dune-1": _torrent_lane(), "classic": _classic()}
    )
    PROVIDERS["p1"] = stub
    gk = _warm_movie_gk(client)

    r = client.get(
        f"/Stream/{gk}/vtt", headers={"X-Emby-Token": TOKEN}, follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == (
        "http://bitplay.lan:3347/api/v1/torrent/abc/stream/7?format=vtt"
    )

    # The /Videos/ spelling serves the same route (DeliveryUrl alt form).
    r2 = client.get(
        f"/Videos/{gk}/vtt", headers={"X-Emby-Token": TOKEN}, follow_redirects=False
    )
    assert r2.status_code == 302

    # No subtitle on the session → the standing 404, never a 5xx.
    r3 = client.get("/Stream/p1:classic/vtt", headers={"X-Emby-Token": TOKEN})
    assert r3.status_code == 404


# ------------------------------------------------- Ukrainian-lane parity


def test_ukrainian_lane_playback_bytes_pinned_unchanged(client: TestClient) -> None:
    """PARITY GATE: a classic (Ukrainian-lane) PlaybackInfo response must
    stay byte-identical to the pre-#378 wire. The frozen fixture pins the
    exact JSON; any drift in the thin-source shape — a new key, a
    reordered stream, an unrequested DeliveryUrl — fails here."""
    stub = _PlaybackStub(streams={"dune-1": _classic()})
    PROVIDERS["p1"] = stub
    gk = _warm_movie_gk(client)

    body = _post(client, f"/Items/{gk}/PlaybackInfo")
    # Sort keys: PlaySessionId is a fresh UUID per request — swap BOTH
    # occurrences for the fixture's placeholder before comparing.
    actual = json.loads(json.dumps(body, sort_keys=True))
    uuid_actual = actual["MediaSources"][0]["PlaySessionId"]
    assert actual["PlaySessionId"] == uuid_actual
    actual["MediaSources"][0]["PlaySessionId"] = "<uuid>"
    actual["PlaySessionId"] = "<uuid>"

    expected = json.loads((FIX / "playbackinfo_ukrainian_pin.json").read_text(encoding="utf-8"))
    assert actual == expected, "Ukrainian-lane PlaybackInfo wire moved — parity gate (#378)"
