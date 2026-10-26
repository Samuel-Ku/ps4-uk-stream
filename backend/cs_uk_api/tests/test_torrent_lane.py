"""Torrent lane conversation pins — the policy moved here from
test_yts.py with its module (providers/yts.py -> torrent_lane.py):
pure candidate parsing, the deterministic quality pick, the fork's
magnet convention, and the lane's playable-content-id grammar. The
provider-facing stream/fallback pins stay in test_yts_stream.py; the
popcorn-dialect payload pins stay in test_yts.py.
"""
import pytest

from cs_uk_api.models import StreamResponse
from cs_uk_api.providers.base import ProviderError
from cs_uk_api.torrent_engine import (
    EnginePathError,
    EngineRejected,
    EngineStream,
    EngineUnavailable,
)
from cs_uk_api.torrent_lane import (
    TorrentCandidate,
    build_magnet,
    parse_stream_content_id,
    play_torrent,
    select_torrent,
    torrent_candidates,
)

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
    cands = torrent_candidates(movie["torrents"])
    assert cands == [
        _cand("1080p", "H_A", 50),
        _cand("1080p", "H_B", 200),
        _cand("720p", "H_C", 0),
    ]


# ---------------------------------------------------------------------------
# The lane's playable-content-id grammar (g3-card playback fix)
# ---------------------------------------------------------------------------


def test_parse_stream_content_id_accepts_every_movie_spelling():
    """One grammar, three movie spellings, one play: the sentinel form
    (native /api/stream + the #378 wire), the BARE IMDb code (what the
    facade hands over when the user plays the ``g3:`` search card — D6:
    every provider takes the bare external), and the provider-scoped
    bare id (the search card's own item id)."""
    for cid in (
        "tt1160419:__movie__",
        "yts:tt1160419:__movie__",
        "tt1160419",
        "yts:tt1160419",
    ):
        assert parse_stream_content_id(cid) == ("tt1160419", None), cid


def test_parse_stream_content_id_episode_tails_keep_their_season():
    """The #379 episode forms ride the same grammar: the tail's SEASON
    is the discriminator, provider prefix or not (``s1e2`` = season 1)."""
    assert parse_stream_content_id("tt1160419:s1e2") == ("tt1160419", 1)
    assert parse_stream_content_id("yts:tt1160419:s1e2") == ("tt1160419", 1)
    assert parse_stream_content_id("yts:tt1160419:s2e10") == ("tt1160419", 2)


@pytest.mark.parametrize(
    "cid",
    ["garbage", "yts:", "p1:dune-1", "g3:tt1160419", "tt1160419:s9e99x"],
)
def test_parse_stream_content_id_refuses_the_rest_loudly(cid: str):
    """Everything else is the lane's typed item verdict ``not_found``
    (ADR-0002) — never a lane fault, never a silent None."""
    with pytest.raises(ProviderError) as exc:
        parse_stream_content_id(cid)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# play_torrent — the lane's engine-facing interface
# ---------------------------------------------------------------------------


class _FutureEnginePathFault(EnginePathError):
    """A future engine-transport subclass; the lane catches the marker."""


class _OutcomeEngine:
    """Scripted engine: each call raises the next exception or returns the
    next stream, and records exactly what the lane handed it."""

    def __init__(self, outcomes: list[EngineStream | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, str | None]] = []

    async def ensure_session(
        self, identifier: str, *, file_hint: str | None = None
    ) -> EngineStream:
        self.calls.append((identifier, file_hint))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_play_torrent_returns_wire_shape_and_file_hint() -> None:
    """The lane returns the wire response, not the engine session, and the
    file hint rides every engine call."""
    session = EngineStream(
        url="http://bitplay.lan:3347/api/v1/torrent/abc/stream/5",
        container="mp4",
        seekable=False,
        subtitle_url="http://bitplay.lan:3347/api/v1/torrent/abc/stream/6?format=vtt",
    )
    engine = _OutcomeEngine([session])

    result = await play_torrent(
        engine,
        [_cand("1080p", "H1080", 5)],
        file_hint="s01e",
    )

    assert result == StreamResponse(
        url=session.url,
        type="mp4",
        headers={},
        seekable=False,
        subtitle_url=session.subtitle_url,
    )
    assert engine.calls == [(build_magnet("H1080"), "s01e")]


@pytest.mark.asyncio
async def test_play_torrent_falls_back_after_rejection_and_keeps_file_hint() -> None:
    """A dead swarm advances to the next policy candidate with the same
    season file hint; the second candidate serves the stream."""
    session = EngineStream(url="http://lan/good", container="mp4")
    engine = _OutcomeEngine([EngineRejected("metadata timeout"), session])

    result = await play_torrent(
        engine,
        [_cand("1080p", "H1080", 5), _cand("720p", "H720", 50)],
        file_hint="s02e",
    )

    assert result.url == session.url
    assert engine.calls == [
        (build_magnet("H1080"), "s02e"),
        (build_magnet("H720"), "s02e"),
    ]


@pytest.mark.asyncio
async def test_play_torrent_all_rejected_is_item_not_found() -> None:
    """Exhausting the bounded fallback is the item's deterministic
    ``not_found``, never an engine fault."""
    engine = _OutcomeEngine(
        [EngineRejected("metadata timeout"), EngineRejected("zero seeders")]
    )

    with pytest.raises(ProviderError) as exc:
        await play_torrent(
            engine,
            [_cand("1080p", "H1080", 5), _cand("720p", "H720", 50)],
            file_hint=None,
        )

    assert exc.value.code == "not_found"
    assert exc.value.message == "no seeders or dead torrent"
    assert exc.value.engine_path is False
    assert len(engine.calls) == 2


@pytest.mark.asyncio
async def test_play_torrent_empty_candidates_is_defensive_not_found() -> None:
    """The provider owns the specific no-torrents message; an empty list
    still gets the lane's typed item verdict, without an engine call."""
    engine = _OutcomeEngine([])

    with pytest.raises(ProviderError) as exc:
        await play_torrent(engine, [], file_hint=None)

    assert exc.value.code == "not_found"
    assert exc.value.message == "no seeders or dead torrent"
    assert engine.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [EngineUnavailable("connection refused"), _FutureEnginePathFault("tls failure")],
    ids=["engine-unavailable", "engine-path-marker"],
)
async def test_play_torrent_aborts_on_any_engine_path_fault(fault: Exception) -> None:
    """Every EnginePathError, not just today's concrete subclass, aborts
    the fallback and becomes the tagged ``unreachable`` ProviderError."""
    engine = _OutcomeEngine([fault])

    with pytest.raises(ProviderError) as exc:
        await play_torrent(
            engine,
            [_cand("1080p", "H1080", 5), _cand("720p", "H720", 50)],
            file_hint=None,
        )

    assert exc.value.code == "unreachable"
    assert exc.value.message == f"torrent engine unreachable: {fault}"
    assert exc.value.engine_path is True
    assert exc.value.__cause__ is fault
    assert engine.calls == [(build_magnet("H1080"), None)]
