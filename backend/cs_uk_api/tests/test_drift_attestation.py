"""Playback-axis tests (drift monitor, spec #285's blind spot).

The axis reads the API's health surface and applies the prober's discipline to
the other half of the question: the first sweep in which a provider's streams
did not play logs only, the second consecutive one files an issue, and a sweep
whose plays happen again closes it.

What is pinned here is the WINDOW's arithmetic and the surface read — the two
places a wrong answer is silent. The sweep-level flow (filing, dedupe, closing,
the two axes staying independent) lives in ``test_drift_monitor.py`` beside the
probe axis it mirrors.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cs_uk_api.drift.attestation import (
    DEFAULT_HEALTH_URL,
    UNKNOWN_PROVIDER_KEY,
    PlayCounters,
    PlaySnapshot,
    fetch_play_attestation,
    play_verdict,
    zero_progress_items,
)
from cs_uk_api.models import PlayFault


def _counters(served: int, no_progress: int = 0) -> PlayCounters:
    return PlayCounters(served=served, no_progress=no_progress)


# ------------------------------------------------------------ the window


def test_a_first_observation_cannot_accuse() -> None:
    """No previous counters = nothing to compare, whatever this sweep shows.

    The surface reports process-lifetime totals, so the FIRST sweep that ever
    sees a provider (or the first after the state file was lost) has no window
    and must not file — otherwise every fresh install would accuse.
    """
    verdict = play_verdict(None, _counters(served=9, no_progress=9))

    assert verdict.kind == "no-evidence"
    assert "first observation" in verdict.reason


def test_no_traffic_is_not_evidence() -> None:
    """A provider nobody played from is neither innocent nor guilty."""
    verdict = play_verdict(_counters(7, 7), _counters(7, 7))

    assert verdict.kind == "no-evidence"
    assert "no stream was served" in verdict.reason


def test_counters_going_backwards_are_a_restart_not_negative_plays() -> None:
    """The API restarted: its counters went back, so the window is unknown.

    Read as a delta this would be negative; read as evidence it would be a
    fault. It is neither — the sweep after a restart starts a new window.
    """
    verdict = play_verdict(_counters(40, 40), _counters(1, 1))

    assert verdict.kind == "no-evidence"
    assert "restarted" in verdict.reason


def test_every_served_stream_stopping_at_zero_is_the_fault() -> None:
    """The fault this axis exists for: served, and never played."""
    verdict = play_verdict(_counters(2, 2), _counters(5, 5))

    assert verdict.kind == "no-progress"
    assert verdict.served_delta == 3
    assert verdict.no_progress_delta == 3
    assert "stopped at zero progress" in verdict.reason


def test_a_stream_that_is_still_playing_is_clean() -> None:
    """Only a STOP reports zero progress, so an in-flight play proves nothing
    is wrong — the case that keeps an actively watched provider out of the
    fault even though its earlier plays may have ended badly."""
    verdict = play_verdict(_counters(1, 1), _counters(4, 1))

    assert verdict.kind == "clean"
    assert verdict.served_delta == 3
    assert verdict.no_progress_delta == 0


def test_some_plays_playing_is_clean() -> None:
    """A bad title among working ones is not a broken provider.

    One stop for five served streams is what a single dead upstream URL (or a
    client hiccup) looks like; the provider is demonstrably playing.
    """
    verdict = play_verdict(_counters(10, 2), _counters(15, 3))

    assert verdict.kind == "clean"
    assert "playback is happening" in verdict.reason


def test_more_reports_than_plays_still_faults() -> None:
    """A stop reported in this window for a stream served in the last one is
    still a served stream that did not play; the comparison is on counts, not
    on pairing them up."""
    verdict = play_verdict(_counters(5, 0), _counters(5, 1))

    assert verdict.kind == "no-evidence"  # nothing served this window
    # ... and with traffic alongside it, it faults.
    verdict = play_verdict(_counters(5, 0), _counters(6, 1))
    assert verdict.kind == "no-progress"


# ------------------------------------------------------------ the counters


def test_counters_round_trip_through_state() -> None:
    counters = _counters(4, 2)

    assert PlayCounters.from_dict(counters.to_dict()) == counters


def test_unreadable_counters_raise_for_the_state_ladder() -> None:
    """A bad value must raise so the store degrades to a fresh state rather
    than silently reading as zero plays (which would acquit a broken lane)."""
    with pytest.raises(TypeError):
        PlayCounters.from_dict([1, 2])


# ------------------------------------------------------------ the surface


class _Response:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _HealthHttp:
    """httpx-like client whose GET answers the health surface."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self._status = status_code
        self.urls: list[str] = []

    async def get(self, url: str) -> _Response:
        self.urls.append(url)
        return _Response(self._status, self._payload)


class _DeadHttp:
    async def get(self, url: str) -> _Response:
        raise httpx.ConnectError("connection refused")


def _health_payload(served: int = 5, no_progress: int = 3) -> dict[str, Any]:
    return {
        "providers": {"animeon": {"status": "ok"}},
        "playback": {
            "providers": {
                "animeon": {
                    "served": served,
                    "refused": 2,
                    "refused_codes": {"gated": 2},
                    "no_progress": no_progress,
                }
            },
            "recent": [
                {
                    "at": "2026-10-26T03:00:00+00:00",
                    "outcome": "no-progress",
                    "provider": "animeon",
                    "item_id": "animeon:111",
                },
                {
                    "at": "2026-10-26T03:01:00+00:00",
                    "outcome": "refused",
                    "provider": "animeon",
                    "item_id": "animeon:222",
                    "code": "gated",
                    "surface": "stream",
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_the_surface_answers_with_counters_and_the_tail() -> None:
    http = _HealthHttp(_health_payload(served=5, no_progress=3))

    snapshot, error = await fetch_play_attestation(DEFAULT_HEALTH_URL, http)  # type: ignore[arg-type]

    assert error is None
    assert snapshot is not None
    assert snapshot.providers["animeon"] == _counters(5, 3)
    assert [f.item_id for f in snapshot.recent] == ["animeon:111", "animeon:222"]
    assert http.urls == [DEFAULT_HEALTH_URL]


@pytest.mark.asyncio
async def test_an_api_that_is_down_is_no_evidence_not_a_fault() -> None:
    """The monitor must never file an issue because the API was unreachable:
    an unreadable surface has to degrade to "nothing observed"."""
    snapshot, error = await fetch_play_attestation(
        DEFAULT_HEALTH_URL, _DeadHttp()  # type: ignore[arg-type]
    )

    assert snapshot is None
    assert error is not None and "ConnectError" in error


@pytest.mark.asyncio
async def test_a_non_200_surface_names_its_status() -> None:
    snapshot, error = await fetch_play_attestation(
        DEFAULT_HEALTH_URL,
        _HealthHttp({}, status_code=503),  # type: ignore[arg-type]
    )

    assert snapshot is None
    assert error == "HTTP 503"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},  # no playback key at all
        {"playback": None},
        {"playback": {"providers": "not-a-map"}},
        {"playback": {"providers": {"animeon": {"served": "many"}}}},
    ],
)
async def test_a_moved_payload_is_no_evidence(payload: dict[str, Any]) -> None:
    """The surface is another process's contract and may move before the
    monitor is updated; an unknown shape must read as nothing observed."""
    snapshot, error = await fetch_play_attestation(
        DEFAULT_HEALTH_URL, _HealthHttp(payload)  # type: ignore[arg-type]
    )

    assert snapshot is None
    assert error is not None and error.startswith("unreadable payload")


def test_zero_progress_items_keeps_the_fault_tail_only() -> None:
    """The issues' triage ids come from the tail, filtered to THIS provider's
    zero-progress reports — a refusal is a different verdict and another
    provider's items are another lane."""
    snapshot = PlaySnapshot(
        providers={UNKNOWN_PROVIDER_KEY: _counters(1, 1)},
        recent=[
            PlayFault(
                at="2026-10-26T03:00:00+00:00",
                outcome="no-progress",
                provider="animeon",
                item_id="animeon:111",
            ),
            PlayFault(
                at="2026-10-26T03:01:00+00:00",
                outcome="refused",
                provider="animeon",
                item_id="animeon:222",
                code="gated",
                surface="stream",
            ),
            PlayFault(
                at="2026-10-26T03:02:00+00:00",
                outcome="no-progress",
                provider="uakino",
                item_id="uakino:333",
            ),
        ],
    )

    assert zero_progress_items(snapshot, "animeon") == ["animeon:111"]
    assert zero_progress_items(snapshot, "uakino") == ["uakino:333"]
    assert zero_progress_items(snapshot, "nowhere") == []
