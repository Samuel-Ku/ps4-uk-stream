"""TorrentEngine seam tests (spec #374, ticket #375).

The seam is the ONE new interface of the English-content lane: ensure a
playable session for an opaque identifier and receive back a plain LAN
URL plus its container. Tests cover the contract pieces this module
owns: the exception vocabulary, the deterministic in-memory fake (call
recording for later route-level orchestration assertions), the real
BitPlay HTTP adapter against respx-mocked endpoints, and the settings
builder (unset url ⇒ None ⇒ lane disabled).
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
import respx

from cs_uk_api import config as _config
from cs_uk_api.torrent_engine import (
    BitPlayClient,
    build_engine_from_settings,
    EngineRejected,
    EngineStream,
    EngineUnavailable,
    FakeTorrentEngine,
    get_engine,
    reset_engine,
    TorrentEngine,
    TorrentEngineError,
)

_MAGNET = "magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel"

_BASE = "http://bitplay.lan:3347"
_ADD = f"{_BASE}/api/v1/torrent/add"
_SESSION = "08ada5a7a6183aae1e09d831df6748d566095a10"
_FILES = f"{_BASE}/api/v1/torrent/{_SESSION}"


def _client(**kwargs: str) -> BitPlayClient:
    return BitPlayClient(base_url=_BASE, **kwargs)


def _mock_add(session_id: str | None = _SESSION, status: int = 200) -> respx.Route:
    payload = {"sessionId": session_id} if session_id is not None else {"error": "??"}
    return respx.post(_ADD).mock(return_value=httpx.Response(status, json=payload))


def _mock_files(entries: list[dict[str, object]], status: int = 200) -> respx.Route:
    return respx.get(_FILES).mock(return_value=httpx.Response(status, json=entries))


# --------------------------------------------------------- exceptions


def test_exception_vocabulary_routes_under_one_base() -> None:
    """Route layers map these onto the provider error taxonomy later;
    both leaf cases must stay distinguishable under the base class."""
    assert issubclass(EngineUnavailable, TorrentEngineError)
    assert issubclass(EngineRejected, TorrentEngineError)
    assert issubclass(TorrentEngineError, Exception)


# ---------------------------------------------------------------- fake


async def test_fake_is_assignable_to_protocol() -> None:
    engine: TorrentEngine = FakeTorrentEngine()
    stream = await engine.ensure_session(_MAGNET)
    assert stream.container == "mp4"
    assert stream.url.startswith("http://")


async def test_fake_deterministic_same_identifier_same_stream() -> None:
    engine = FakeTorrentEngine()
    first = await engine.ensure_session(_MAGNET)
    second = await engine.ensure_session(_MAGNET)
    assert first == second


async def test_fake_distinct_identifiers_distinct_streams() -> None:
    engine = FakeTorrentEngine()
    a = await engine.ensure_session("magnet:?xt=urn:btih:aaa")
    b = await engine.ensure_session("magnet:?xt=urn:btih:bbb")
    assert a.url != b.url


async def test_fake_configured_mapping_honored() -> None:
    configured = EngineStream(url="http://lan-host:3347/s/abc", container="mp4")
    engine = FakeTorrentEngine(streams={_MAGNET: configured})
    assert await engine.ensure_session(_MAGNET) == configured


async def test_fake_records_call_count_and_last_identifier() -> None:
    engine = FakeTorrentEngine()
    other = "magnet:?xt=urn:btih:bbbb"
    await engine.ensure_session(_MAGNET)
    await engine.ensure_session(other)
    assert engine.ensure_count == 2
    assert engine.last_identifier == other


async def test_fake_ignores_file_hint_deterministically() -> None:
    """The fake has no file model — with or without a hint the SAME
    deterministic stream comes back (route tests key on identity, not
    selection)."""
    engine = FakeTorrentEngine()
    hinted = await engine.ensure_session(_MAGNET, file_hint="Sintel.mp4")
    bare = await engine.ensure_session(_MAGNET)
    assert hinted == bare


# --------------------------------------------------------- BitPlayClient


async def test_bitplay_native_mp4_served_direct() -> None:
    with respx.mock:
        add = _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        stream = await _client().ensure_session(_MAGNET)
    # Native byte-serving is Go http.ServeContent → full Range support
    # (research #367 §1): seekable defaults True and stays True here.
    assert stream == EngineStream(
        url=f"{_FILES}/stream/0", container="mp4", seekable=True
    )
    sent = json.loads(add.calls.last.request.read())
    assert sent == {"Magnet": _MAGNET}


async def test_bitplay_mkv_remuxed_to_mp4() -> None:
    """MKV is not served byte-native — the engine remuxes on the fly and
    the player receives a progressive mp4 URL (spec user story 8)."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mkv", "size": 100}])
        stream = await _client().ensure_session(_MAGNET)
    # The remux endpoint is chunked fMP4 with `Accept-Ranges: none`
    # (research #367 §1) — playable forward but NOT seekable; the flag
    # records that verdict on the seam value itself.
    assert stream == EngineStream(
        url=f"{_FILES}/remux/0", container="mp4", seekable=False
    )


async def test_engine_stream_seekable_defaults_true() -> None:
    """A plain EngineStream (fake-synthesized, native containers) is
    seekable unless an adapter says otherwise."""
    assert EngineStream(url="http://x/v", container="mp4").seekable is True


async def test_fake_streams_are_seekable_by_default() -> None:
    engine = FakeTorrentEngine()
    stream = await engine.ensure_session(_MAGNET)
    assert stream.seekable is True


async def test_bitplay_file_hint_selects_matching_file() -> None:
    entries = [
        {"index": 0, "name": "Sample/trailer.mp4"},
        {"index": 1, "name": "Movie/Movie.mkv"},
    ]
    with respx.mock:
        _mock_add()
        _mock_files(entries)
        hinted_mkv = await _client().ensure_session(_MAGNET, file_hint="MOVIE")
        hinted_trailer = await _client().ensure_session(_MAGNET, file_hint="trailer")
    assert hinted_mkv.url == f"{_FILES}/remux/1"
    assert hinted_trailer == EngineStream(url=f"{_FILES}/stream/0", container="mp4")


async def test_bitplay_auth_failure_is_engine_unavailable() -> None:
    """Bad credentials are lane-level breakage (every item would fail),
    not a rejection of this torrent."""
    with respx.mock:
        respx.post(_ADD).mock(return_value=httpx.Response(401, text="Unauthorized"))
        with pytest.raises(EngineUnavailable):
            await _client(username="u", password="bad").ensure_session(_MAGNET)


async def test_bitplay_invalid_magnet_rejected() -> None:
    with respx.mock:
        respx.post(_ADD).mock(
            return_value=httpx.Response(400, json={"error": "Invalid magnet link"})
        )
        with pytest.raises(EngineRejected):
            await _client().ensure_session("not-a-magnet")


async def test_bitplay_metadata_timeout_dead_torrent_rejected() -> None:
    """BitPlay answers 504 when metadata never arrives (zero seeders) —
    dead-on-arrival for THIS torrent (spec error-surface note)."""
    with respx.mock:
        respx.post(_ADD).mock(
            return_value=httpx.Response(
                504,
                json={"error": "Timeout getting info - proxy might be blocking BitTorrent traffic"},
            )
        )
        with pytest.raises(EngineRejected):
            await _client().ensure_session(_MAGNET)


async def test_bitplay_connection_error_is_unavailable() -> None:
    with respx.mock:
        respx.post(_ADD).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(EngineUnavailable):
            await _client().ensure_session(_MAGNET)


async def test_bitplay_read_timeout_is_unavailable() -> None:
    with respx.mock:
        _mock_add()
        respx.get(_FILES).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(EngineUnavailable):
            await _client().ensure_session(_MAGNET)


async def test_bitplay_sends_basic_auth_when_pair_configured() -> None:
    with respx.mock:
        add = _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4"}])
        await _client(username="admin", password="secret").ensure_session(_MAGNET)
    request = add.calls.last.request
    assert request.headers["Authorization"].startswith("Basic ")


async def test_bitplay_no_auth_header_without_credentials() -> None:
    with respx.mock:
        add = _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4"}])
        await _client().ensure_session(_MAGNET)
    assert "authorization" not in add.calls.last.request.headers


async def test_bitplay_malformed_add_response_is_unavailable() -> None:
    with respx.mock:
        respx.post(_ADD).mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"})
        )
        with pytest.raises(EngineUnavailable):
            await _client().ensure_session(_MAGNET)


async def test_bitplay_files_error_is_unavailable() -> None:
    with respx.mock:
        _mock_add()
        _mock_files([], status=404)
        with pytest.raises(EngineUnavailable):
            await _client().ensure_session(_MAGNET)


# ------------------------------------------------------------- builder


def test_builder_none_when_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _config, "SETTINGS", replace(_config.SETTINGS, torrent_engine_url=None)
    )
    assert build_engine_from_settings() is None


def test_builder_none_when_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit empty string disables the lane exactly like unset
    (the llm-knob convention); the call site decides how to be loud."""
    monkeypatch.setattr(
        _config, "SETTINGS", replace(_config.SETTINGS, torrent_engine_url="")
    )
    assert build_engine_from_settings() is None


def test_builder_builds_bitplay_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _config,
        "SETTINGS",
        replace(_config.SETTINGS, torrent_engine_url="http://bitplay.lan:3347/"),
    )
    engine = build_engine_from_settings()
    assert isinstance(engine, BitPlayClient)


async def test_builder_wires_url_and_auth_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the one settings binding: base URL (trailing
    slash tolerated) and basic-auth pair reach the adapter's requests."""
    monkeypatch.setattr(
        _config,
        "SETTINGS",
        replace(
            _config.SETTINGS,
            torrent_engine_url="http://bitplay.lan:3347/",
            torrent_engine_user="admin",
            torrent_engine_password="secret",
        ),
    )
    engine = build_engine_from_settings()
    assert engine is not None
    with respx.mock:
        add = respx.post(f"{_BASE}/api/v1/torrent/add").mock(
            return_value=httpx.Response(200, json={"sessionId": _SESSION})
        )
        respx.get(_FILES).mock(
            return_value=httpx.Response(200, json=[{"index": 0, "name": "M.mkv"}])
        )
        stream = await engine.ensure_session(_MAGNET)
    assert stream.url == f"{_FILES}/remux/0"
    assert add.calls.last.request.headers["Authorization"].startswith("Basic ")


# ------------------------------------------------- lazy cached singleton


@pytest.fixture(autouse=True)
def _fresh_engine_singleton() -> object:
    """Every singleton test starts and ends with a cold accessor."""
    reset_engine()
    yield
    reset_engine()


def test_get_engine_builds_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _config,
        "SETTINGS",
        replace(_config.SETTINGS, torrent_engine_url="http://bitplay.lan:3347"),
    )
    engine = get_engine()
    assert isinstance(engine, BitPlayClient)


def test_get_engine_caches_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors http_client._client: built once, then the SAME instance on
    every call — settings are re-read only after an explicit reset."""
    monkeypatch.setattr(
        _config,
        "SETTINGS",
        replace(_config.SETTINGS, torrent_engine_url="http://bitplay.lan:3347"),
    )
    first = get_engine()
    second = get_engine()
    assert first is second


def test_get_engine_none_while_unconfigured_and_picks_up_late_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset url ⇒ None (lane disabled, call site owns loudness). While
    unconfigured the accessor keeps re-reading settings, so a lane that
    becomes configured later is picked up WITHOUT a process restart."""
    monkeypatch.setattr(
        _config, "SETTINGS", replace(_config.SETTINGS, torrent_engine_url=None)
    )
    assert get_engine() is None
    monkeypatch.setattr(
        _config,
        "SETTINGS",
        replace(_config.SETTINGS, torrent_engine_url="http://bitplay.lan:3347"),
    )
    assert isinstance(get_engine(), BitPlayClient)


def test_reset_engine_forces_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test hook: reset drops the cached instance so the next access
    rebuilds from current settings (and tests can inject fresh state)."""
    monkeypatch.setattr(
        _config,
        "SETTINGS",
        replace(_config.SETTINGS, torrent_engine_url="http://bitplay.lan:3347"),
    )
    first = get_engine()
    reset_engine()
    second = get_engine()
    assert first is not second
