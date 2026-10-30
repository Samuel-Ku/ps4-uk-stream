"""TorrentEngine seam tests (spec #374, ticket #375).

The seam is the ONE new interface of the English-content lane: ensure a
playable session for an opaque identifier and receive back a plain LAN
URL plus its container. Tests cover the contract pieces this module
owns: the exception vocabulary, the deterministic in-memory fake (call
recording for later route-level orchestration assertions), the real
BitPlay HTTP adapter against respx-mocked endpoints, and the settings
builder (unset url ⇒ None ⇒ lane disabled). The adapter's #461
first-bytes probe — a created session's stream must speak before it is
handed over — is pinned here too, including its consequence at the
seam: a silent candidate advances the lane's bounded fallback, and
every probe logs its measured timing (#461 follow-up) so the 5 s window
is re-derivable from live samples.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

import httpx
import pytest
import respx

from cs_uk_api import config as _config
from cs_uk_api.torrent_engine import (
    BitPlayClient,
    EngineRejected,
    EngineStream,
    EngineUnavailable,
    FakeTorrentEngine,
    TorrentEngine,
    TorrentEngineError,
    build_engine_from_settings,
    get_engine,
    reset_engine,
)
from cs_uk_api.torrent_lane import TorrentCandidate, play_torrent

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


#: #461 — every stream/remux URL the first-bytes probe may read (either
#: endpoint, any file index). One family route keeps the tests asserting
#: the probe's REQUEST, not which file the adapter resolved.
_STREAM_URL_RE = re.compile(
    rf"{re.escape(_BASE)}/api/v1/torrent/[^/]+/(?:stream|remux)/\d+"
)


def _mock_stream(body: bytes = b"\x00" * 65536, status: int = 206) -> respx.Route:
    """The probe's healthy answer, in the gate's own shape: a 206 with
    the requested 64 KiB (``deploy/smoke.sh`` ``-r 0-65535``)."""
    return respx.get(url=_STREAM_URL_RE).mock(
        return_value=httpx.Response(status, content=body)
    )


def _probe_samples(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    """The probe's logged samples, parsed ``key=value`` → dict.

    Doubles as the line-shape pin (#461 follow-up): one line per probe,
    prefix ``first-bytes probe``, every field space-separated and
    ``=``-joined.
    """
    samples: list[dict[str, str]] = []
    for record in caplog.records:
        parts = record.getMessage().split()
        if parts[:2] != ["first-bytes", "probe"]:
            continue
        samples.append(dict(part.split("=", 1) for part in parts[2:]))
    return samples


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
        _mock_stream()
        stream = await _client().ensure_session(_MAGNET)
    # Native byte-serving is Go http.ServeContent → full Range support
    # (research #367 §1): seekable defaults True and stays True here.
    assert stream == EngineStream(
        url=f"{_FILES}/stream/0", container="mp4", seekable=True
    )
    sent = json.loads(add.calls.last.request.read())
    assert sent == {"Magnet": _MAGNET}


async def test_bitplay_mkv_served_natively() -> None:
    """MKV takes BitPlay's native byte endpoint, not the broken remux.

    The engine's fMP4 remux is not playable in Switchfin (it delivers an
    init/header and stalls), while ``/stream/`` serves the original
    Matroska bytes through Go ``http.ServeContent``: seekable, and
    demuxed by the player's own MKV support.
    """
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mkv", "size": 100}])
        _mock_stream()
        stream = await _client().ensure_session(_MAGNET)
    assert stream == EngineStream(
        url=f"{_FILES}/stream/0", container="mkv", seekable=True
    )


async def test_bitplay_avi_still_remuxed_to_mp4() -> None:
    """Only native-capable containers bypass the remuxer: an AVI has no
    byte-native endpoint and keeps the historical mp4 remux."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.avi", "size": 100}])
        _mock_stream()
        stream = await _client().ensure_session(_MAGNET)
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
        _mock_stream()
        hinted_mkv = await _client().ensure_session(_MAGNET, file_hint="MOVIE")
        hinted_trailer = await _client().ensure_session(_MAGNET, file_hint="trailer")
    assert hinted_mkv == EngineStream(
        url=f"{_FILES}/stream/1", container="mkv", seekable=True
    )
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
        with pytest.raises(EngineRejected) as exc:
            await _client().ensure_session("not-a-magnet")
    assert exc.value.retryable is False  # malformed magnet is final


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
        with pytest.raises(EngineRejected) as exc:
            await _client().ensure_session(_MAGNET)
    assert exc.value.retryable is True  # 504 = metadata timeout, retry same magnet


async def test_bitplay_add_read_timeout_is_retryable_torrent_rejection() -> None:
    """A read timeout ON THE ADD CALL itself means the engine accepted
    the connection and blocked waiting for metadata that never arrived
    inside our window (upstream cap 3 min > ADD_TIMEOUT_S). The engine
    may still be resolving that magnet, so the lane gets the retryable
    flag and adds the SAME torrent once more before walking away."""
    with respx.mock:
        respx.post(_ADD).mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(EngineRejected) as exc:
            await _client().ensure_session(_MAGNET)
    assert exc.value.retryable is True


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
        _mock_stream()
        await _client(username="admin", password="secret").ensure_session(_MAGNET)
    request = add.calls.last.request
    assert request.headers["Authorization"].startswith("Basic ")


async def test_bitplay_no_auth_header_without_credentials() -> None:
    with respx.mock:
        add = _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4"}])
        _mock_stream()
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


# --------------------------------------------- #461: the first-bytes
# --------------------------------------- probe (no-bytes swarm verdict)


async def test_bitplay_stream_that_never_speaks_is_torrent_rejected() -> None:
    """A created session (metadata arrived, files listed) can still sit
    on a swarm that sends nothing. Live form (``g3:tt35969257``,
    2026-10-26/27): PlaybackInfo handed a MediaSource, then the reader
    timed out with ``000 0``. The probe turns that into THIS torrent's
    verdict, so the lane's bounded fallback advances instead of the
    player stalling."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        _mock_stream(body=b"", status=200)
        with pytest.raises(EngineRejected) as exc:
            await _client().ensure_session(_MAGNET)
    assert exc.value.retryable is True  # just-created session may still connect


async def test_bitplay_probe_timeout_is_torrent_rejected() -> None:
    """The stall shape itself: the engine accepts the request and never
    sends any response bytes inside the probe window."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        respx.get(url=_STREAM_URL_RE).mock(
            side_effect=httpx.ReadTimeout("stream stalled")
        )
        with pytest.raises(EngineRejected):
            await _client().ensure_session(_MAGNET)


async def test_bitplay_probe_refuses_a_non_200_206_stream() -> None:
    """Anything but the gate's 206/200 answer is silence, even when a
    body rides along."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        _mock_stream(status=500, body=b"boom")
        with pytest.raises(EngineRejected):
            await _client().ensure_session(_MAGNET)


async def test_bitplay_probe_reads_one_bounded_range() -> None:
    """The probe measures the gate's own first bytes: a ranged GET for
    ``bytes=0-65535``, abandoned after the first chunk — never a
    whole-file read against the engine."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        probe = _mock_stream()
        result = await _client().ensure_session(_MAGNET)
    assert result.url == f"{_FILES}/stream/0"
    assert probe.calls.last.request.method == "GET"
    assert probe.calls.last.request.headers["range"] == "bytes=0-65535"


# ------------------- #461 follow-up: the probe's own timing samples


async def test_probe_logs_the_measured_first_byte_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The probe's timing IS the data the 5 s window gets re-derived
    from: a healthy read logs one sample carrying the measured seconds,
    the window it ran under, and the URL it read."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        _mock_stream()
        caplog.set_level(logging.INFO, logger="cs_uk_api.torrent_engine")
        result = await _client().ensure_session(_MAGNET)
    assert result.url == f"{_FILES}/stream/0"
    (sample,) = _probe_samples(caplog)
    assert sample["outcome"] == "ok"
    assert sample["budget_s"] == "5.0"  # self-describing: window in force
    assert sample["url"] == result.url
    assert 0.0 <= float(sample["first_byte_s"]) < 5.0


async def test_probe_logs_a_stalled_read_as_a_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sample that would argue for a WIDER window: the read died on
    the budget itself. Named ``timeout`` so it stays distinguishable
    from a byte-less answer and from a transport fault."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        respx.get(url=_STREAM_URL_RE).mock(
            side_effect=httpx.ReadTimeout("stream stalled")
        )
        caplog.set_level(logging.INFO, logger="cs_uk_api.torrent_engine")
        with pytest.raises(EngineRejected):
            await _client().ensure_session(_MAGNET)
    (sample,) = _probe_samples(caplog)
    assert sample["outcome"] == "timeout"
    assert sample["error"] == "ReadTimeout"
    assert sample["budget_s"] == "5.0"


async def test_probe_logs_a_status_refusal_with_its_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-200/206 answer is not a timing sample at all — the line
    says ``status``, so an engine refusal is never read as a
    slow-but-alive swarm when the distribution is reviewed."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        _mock_stream(status=500, body=b"boom")
        caplog.set_level(logging.INFO, logger="cs_uk_api.torrent_engine")
        with pytest.raises(EngineRejected):
            await _client().ensure_session(_MAGNET)
    (sample,) = _probe_samples(caplog)
    assert sample["outcome"] == "status"
    assert sample["status"] == "500"


async def test_probe_logs_a_byte_less_200_as_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The live ``g3:tt35969257`` shape: a 200/206 whose body ends
    without a byte. Distinct from ``timeout`` (the window never bound)
    and from ``transport`` (the engine itself never answered)."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        _mock_stream(body=b"", status=200)
        caplog.set_level(logging.INFO, logger="cs_uk_api.torrent_engine")
        with pytest.raises(EngineRejected):
            await _client().ensure_session(_MAGNET)
    (sample,) = _probe_samples(caplog)
    assert sample["outcome"] == "empty"


async def test_probe_logs_a_transport_fault_as_such(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused connection is engine-path trouble, never a swarm
    verdict — the sample says ``transport`` so it is not counted as a
    dead candidate when the samples are reviewed."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 0, "name": "Movie.mp4", "size": 100}])
        respx.get(url=_STREAM_URL_RE).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        caplog.set_level(logging.INFO, logger="cs_uk_api.torrent_engine")
        with pytest.raises(EngineRejected):
            await _client().ensure_session(_MAGNET)
    (sample,) = _probe_samples(caplog)
    assert sample["outcome"] == "transport"
    assert sample["error"] == "ConnectError"


async def test_no_bytes_session_advances_to_the_next_candidate() -> None:
    """#461's acceptance, end to end at the seam: the REAL adapter
    rejects the first candidate's silent session and the lane's bounded
    fallback plays the second — what the smoke gate has been doing by
    hand for ``g3:tt35969257`` since 2026-10-26."""
    silent, live = "a" * 40, "b" * 40
    silent_files = f"{_BASE}/api/v1/torrent/{silent}"
    live_files = f"{_BASE}/api/v1/torrent/{live}"
    with respx.mock:
        add = respx.post(_ADD).mock(
            side_effect=[
                httpx.Response(200, json={"sessionId": silent}),
                httpx.Response(200, json={"sessionId": live}),
            ]
        )
        entries = [{"index": 0, "name": "Movie.mp4", "size": 100}]
        respx.get(silent_files).mock(
            return_value=httpx.Response(200, json=entries)
        )
        respx.get(live_files).mock(
            return_value=httpx.Response(200, json=entries)
        )
        respx.get(f"{silent_files}/stream/0").mock(
            return_value=httpx.Response(200, content=b"")
        )
        respx.get(f"{live_files}/stream/0").mock(
            return_value=httpx.Response(206, content=b"\x00" * 65536)
        )
        result = await play_torrent(
            _client(),
            [
                TorrentCandidate(quality="1080p", info_hash="AAAA", seeds=5),
                TorrentCandidate(quality="720p", info_hash="BBBB", seeds=50),
            ],
            file_hint=None,
        )
    assert result.url == f"{live_files}/stream/0"
    assert add.call_count == 2


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
        _mock_stream()
        stream = await engine.ensure_session(_MAGNET)
    assert stream.url == f"{_FILES}/stream/0"
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


# ----------------------------------------------------- #378: what the
# ----------------------------------------------- session actually carries


async def test_bitplay_srt_companion_yields_vtt_url() -> None:
    """#378: an external .srt in the SAME torrent surfaces the engine's
    `?format=vtt` conversion endpoint on the stream value — English-preferred
    when several srts ride along."""
    with respx.mock:
        _mock_add()
        _mock_files(
            [
                {"index": 5, "name": "Sintel.mp4", "size": 129241752},
                {"index": 6, "name": "Sintel.uk.srt", "size": 1514},
                {"index": 7, "name": "Sintel.en.srt", "size": 1499},
            ]
        )
        _mock_stream()
        stream = await _client().ensure_session(_MAGNET)
    assert stream.subtitle_url == f"{_FILES}/stream/7?format=vtt"


async def test_bitplay_single_srt_surfaces_it() -> None:
    with respx.mock:
        _mock_add()
        _mock_files(
            [
                {"index": 5, "name": "Sintel.mp4", "size": 129241752},
                {"index": 6, "name": "Sintel.srt", "size": 1514},
            ]
        )
        _mock_stream()
        stream = await _client().ensure_session(_MAGNET)
    assert stream.subtitle_url == f"{_FILES}/stream/6?format=vtt"


async def test_bitplay_no_srt_means_no_subtitle_url() -> None:
    """The honest 'no subtitles' verdict: the facade advertises only
    external .srt tracks (research #367 limit 6), so its absence must be
    OMITTED, not faked."""
    with respx.mock:
        _mock_add()
        _mock_files([{"index": 5, "name": "Sintel.mp4", "size": 129241752}])
        _mock_stream()
        stream = await _client().ensure_session(_MAGNET)
    assert stream.subtitle_url is None


async def test_bitplay_native_mkv_keeps_subtitle() -> None:
    """The VTT endpoint is INDEPENDENT of the video container choice: a
    native mkv still exposes its external srt companion."""
    with respx.mock:
        _mock_add()
        _mock_files(
            [
                {"index": 0, "name": "Movie.mkv", "size": 100},
                {"index": 1, "name": "Movie.en.srt", "size": 20},
            ]
        )
        _mock_stream()
        stream = await _client().ensure_session(_MAGNET)
    assert stream.url == f"{_FILES}/stream/0"
    assert stream.container == "mkv"
    assert stream.seekable is True
    assert stream.subtitle_url == f"{_FILES}/stream/1?format=vtt"


async def test_fake_carries_full_engine_streams() -> None:
    """The fake's configured table passes EngineStreams through verbatim —
    subtitle_url rides the SAME knob; unconfigured identifiers synthesize
    a bare stream with none (#378 omission default)."""
    rich = EngineStream(
        url="http://lan/s/1",
        container="mp4",
        subtitle_url="http://lan/s/2?format=vtt",
    )
    engine = FakeTorrentEngine(streams={_MAGNET: rich})
    assert await engine.ensure_session(_MAGNET) == rich
    bare = await engine.ensure_session("magnet:?xt=urn:btih:other")
    assert bare.subtitle_url is None
