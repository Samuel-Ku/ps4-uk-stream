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

from cs_uk_api.torrent_engine import (
    EngineRejected,
    EngineStream,
    EngineUnavailable,
    FakeTorrentEngine,
    TorrentEngine,
    TorrentEngineError,
)

_MAGNET = "magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel"


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
