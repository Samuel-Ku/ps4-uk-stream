"""Play attestation: the health surface's playback axis (unit + routes).

``health`` answers "is this LANE sick?" — upstream operation outcomes.
This file pins the other half of the question, the one that used to be a
log line and nothing else: **did a play request get video?** Three
outcomes, and the pins that keep them from silently vanishing:

  - every playback surface records, driven here through its REAL route:
    the native ``/api/stream/{id}``, the facade's ``playbackinfo`` probe,
    its byte ``stream`` route, ``download`` and a HLS ``segment``
    re-resolution. A surface that stops declaring itself is a state the
    backend goes blind to again (``test_every_playback_surface_declares_
    itself`` fails, it does not pass quietly);
  - the subtitle route does NOT record: it crosses the same seam to find
    a track, so counting it would inflate the axis with non-play requests
    (both its refusal and its success are asserted uncounted);
  - item-level verdicts appear ONLY on this axis (ADR-0002): five
    ``gated`` refusals leave the lane's window empty — five being the
    tracker's ``min_samples``, so a lane that DID take the samples would
    report ``down`` and the pin would fail rather than pass by
    arithmetic — while a lane code moves both axes;
  - a client stop whose position never advanced is the third outcome, and
    only the stop report may witness it: a start or a heartbeat
    legitimately begins at zero.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.health import TRACKER
from cs_uk_api.main import app
from cs_uk_api.models import StreamResponse
from cs_uk_api.play_attestation import (
    PLAY_ATTESTATION,
    UNKNOWN_PROVIDER,
    PlayAttestationTracker,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import ProviderError

client = TestClient(app)

TOKEN = SETTINGS.jellyfin_token
HEADERS = {"X-Emby-Token": TOKEN}

PROVIDER = "eneyida"
EPISODE = f"{PROVIDER}:s1e1"
STREAM_URL = "https://cdn.example.test/dune.mp4"


def playback() -> dict[str, Any]:
    """The ``/api/health`` playback axis, read as a client reads it."""
    r = client.get("/api/health")
    assert r.status_code == 200
    return r.json()["playback"]


def stub_stream(monkeypatch: pytest.MonkeyPatch, result: StreamResponse | Exception) -> None:
    """Make the provider's ``stream()`` answer with ``result`` (or raise it)."""

    async def _stream(external_id: str, translation: str | None, http: object) -> StreamResponse:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(PROVIDERS[PROVIDER], "stream", _stream)


# ---------------------------------------------------------------------------
# The tracker (unit)
# ---------------------------------------------------------------------------

def test_outcomes_are_counted_per_provider() -> None:
    t = PlayAttestationTracker()
    t.served(PROVIDER)
    t.served(PROVIDER)
    t.refused(PROVIDER, EPISODE, "gated", "stream")
    t.refused(PROVIDER, EPISODE, "not_found", "playbackinfo")
    t.no_progress(PROVIDER, EPISODE)
    snap = t.snapshot()
    assert snap.providers[PROVIDER].served == 2
    assert snap.providers[PROVIDER].refused == 2
    assert snap.providers[PROVIDER].refused_codes == {"gated": 1, "not_found": 1}
    assert snap.providers[PROVIDER].no_progress == 1


def test_a_codeless_crash_is_counted_under_unknown() -> None:
    t = PlayAttestationTracker()
    t.refused(PROVIDER, EPISODE, None, "native")
    assert t.snapshot().providers[PROVIDER].refused_codes == {"unknown": 1}


def test_an_unresolvable_id_is_its_own_bucket() -> None:
    """No guessing a provider for a malformed id or a cold group key."""
    t = PlayAttestationTracker()
    t.refused(None, "ghost:1", "not_found", "native")
    t.no_progress(None, "g2:cold")
    assert set(t.snapshot().providers) == {UNKNOWN_PROVIDER}


def test_the_recent_tail_is_bounded_and_oldest_first() -> None:
    t = PlayAttestationTracker(recent_max=3)
    for i in range(5):
        t.refused(PROVIDER, f"{PROVIDER}:{i}", "not_found", "stream")
    recent = t.snapshot().recent
    assert [f.item_id for f in recent] == [f"{PROVIDER}:2", f"{PROVIDER}:3", f"{PROVIDER}:4"]
    assert all(f.outcome == "refused" and f.surface == "stream" for f in recent)


def test_a_no_progress_fault_carries_neither_code_nor_surface() -> None:
    t = PlayAttestationTracker()
    t.no_progress(PROVIDER, EPISODE)
    fault = t.snapshot().recent[0]
    assert (fault.outcome, fault.provider, fault.code, fault.surface) == (
        "no-progress",
        PROVIDER,
        None,
        None,
    )


def test_reset_clears_counters_and_the_tail() -> None:
    t = PlayAttestationTracker()
    t.served(PROVIDER)
    t.refused(PROVIDER, EPISODE, "gated", "stream")
    t.no_progress(PROVIDER, EPISODE)
    t.reset()
    snap = t.snapshot()
    assert snap.providers == {} and snap.recent == []


# ---------------------------------------------------------------------------
# The health surface
# ---------------------------------------------------------------------------

def test_health_always_carries_the_playback_axis() -> None:
    """Present and empty at rest — a client can rely on the shape."""
    assert playback() == {"providers": {}, "recent": []}


# ---------------------------------------------------------------------------
# The native surface
# ---------------------------------------------------------------------------

def test_native_served_is_attested(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_stream(monkeypatch, StreamResponse(url=STREAM_URL))
    assert client.get(f"/api/stream/{EPISODE}").status_code == 200
    axis = playback()
    assert axis["providers"][PROVIDER]["served"] == 1
    assert axis["providers"][PROVIDER]["refused"] == 0
    assert axis["recent"] == []


def test_native_refusal_carries_the_code_the_client_saw() -> None:
    assert client.get("/api/stream/ghost:1").status_code == 404
    axis = playback()
    assert axis["providers"][UNKNOWN_PROVIDER]["refused_codes"] == {"not_found": 1}
    fault = axis["recent"][0]
    assert fault["item_id"] == "ghost:1"
    assert (fault["outcome"], fault["provider"], fault["code"], fault["surface"]) == (
        "refused",
        UNKNOWN_PROVIDER,
        "not_found",
        "native",
    )
    assert fault["at"], "a fault without a timestamp cannot be lined up with the log"


def test_native_upstream_fault_is_attested_with_its_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code recorded is the one the CLIENT was handed.

    The native guard answers a lane fault with the canonical 502 envelope
    (``upstream_unreachable``, ADR-0002 spirit: verdicts ≠ infra faults),
    so that is the refusal an operator sees on this axis. The provider's
    own code is not lost — ``record_verdict`` puts ``unreachable`` on the
    LANE axis in the same request, which the test for the two axes pins.
    """
    stub_stream(monkeypatch, ProviderError("unreachable", "site 522"))
    assert client.get(f"/api/stream/{EPISODE}").status_code == 502
    assert playback()["providers"][PROVIDER]["refused_codes"] == {"upstream_unreachable": 1}
    assert TRACKER.last_error_at(PROVIDER) is not None


def test_native_unknown_translation_is_attested(monkeypatch: pytest.MonkeyPatch) -> None:
    async def translations(external_id: str, http: object) -> list[str]:
        return ["Дубляж"]

    stub_stream(monkeypatch, StreamResponse(url=STREAM_URL))
    monkeypatch.setattr(PROVIDERS[PROVIDER], "episode_translations", translations)
    r = client.get(f"/api/stream/{EPISODE}", params={"translation": "Нема"})
    assert r.status_code == 400
    assert playback()["providers"][PROVIDER]["refused_codes"] == {"invalid_translation": 1}


# ---------------------------------------------------------------------------
# The facade surfaces
# ---------------------------------------------------------------------------

def test_every_playback_surface_declares_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """One refused item, four surfaces — each names itself in flow order.

    ``playbackinfo`` is the pre-play probe; ``stream``/``download``/``segment``
    are the byte paths. If a surface stops declaring itself the list loses an
    entry and this fails.
    """
    stub_stream(monkeypatch, ProviderError("gated", "subscription"))
    assert client.get(f"/Items/{EPISODE}/PlaybackInfo", headers=HEADERS).status_code == 404
    assert client.get(f"/Videos/{EPISODE}/stream").status_code == 404
    assert client.get(f"/Items/{EPISODE}/Download").status_code == 404
    assert (
        client.get(
            f"/Videos/{EPISODE}/segment", params={"url": f"{STREAM_URL}?seg=1"}
        ).status_code
        == 404
    )
    axis = playback()
    assert [f["surface"] for f in axis["recent"]] == [
        "playbackinfo",
        "stream",
        "download",
        "segment",
    ]
    assert axis["providers"][PROVIDER]["refused_codes"] == {"gated": 4}


def test_item_verdict_moves_only_the_playback_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ADR-0002 separation, through the real facade route.

    Five ``gated`` refusals — the #373 shape — must leave the lane's health
    window untouched, while all five land on the playback axis. Five is the
    tracker's ``min_samples``: a lane that did take the samples would read
    ``down`` here.
    """
    stub_stream(monkeypatch, ProviderError("gated", "subscription"))
    for _ in range(5):
        assert client.get(f"/Videos/{EPISODE}/stream").status_code == 404
    assert playback()["providers"][PROVIDER]["refused_codes"] == {"gated": 5}
    assert TRACKER.status(PROVIDER) == "ok"
    statuses = {p["id"]: p["status"] for p in client.get("/api/providers").json()}
    assert statuses[PROVIDER] == "ok"


def test_a_lane_fault_moves_both_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The complement of the pin above: a real lane fault is still lane health."""
    stub_stream(monkeypatch, ProviderError("unreachable", "site 522"))
    for _ in range(5):
        assert client.get(f"/Videos/{EPISODE}/stream").status_code == 404
    assert TRACKER.status(PROVIDER) == "down"
    assert playback()["providers"][PROVIDER]["refused_codes"] == {"unreachable": 5}


def test_the_subtitle_route_is_not_a_playback_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subtitles cross the same seam for a track, not for playback."""
    stub_stream(monkeypatch, ProviderError("gated", "subscription"))
    assert client.get(f"/Stream/{EPISODE}/vtt").status_code == 404
    assert playback()["providers"] == {}

    # ...and the other direction: a stream that RESOLVES for a subtitle-less
    # item is still not a play request, so it is not counted as served.
    stub_stream(monkeypatch, StreamResponse(url=STREAM_URL))
    assert client.get(f"/Stream/{EPISODE}/vtt").status_code == 404
    assert playback()["providers"] == {}


# ---------------------------------------------------------------------------
# The client's side: served bytes that never played
# ---------------------------------------------------------------------------

def test_a_client_stop_without_progress_is_the_third_outcome() -> None:
    r = client.post(
        "/Sessions/Playing/Stopped",
        json={"ItemId": EPISODE, "PositionTicks": 0},
        headers=HEADERS,
    )
    assert r.status_code == 204
    axis = playback()
    assert axis["providers"][PROVIDER]["no_progress"] == 1
    fault = axis["recent"][0]
    assert (fault["outcome"], fault["provider"], fault["code"], fault["surface"]) == (
        "no-progress",
        PROVIDER,
        None,
        None,
    )


def test_only_a_stop_report_can_witness_zero_progress() -> None:
    """A start and a heartbeat legitimately begin at zero; a later stop at
    a real position is a play that worked."""
    client.post(
        "/Sessions/Playing", json={"ItemId": EPISODE, "PositionTicks": 0}, headers=HEADERS
    )
    client.post(
        "/Sessions/Playing/Progress",
        json={"ItemId": EPISODE, "PositionTicks": 0},
        headers=HEADERS,
    )
    client.post(
        "/Sessions/Playing/Stopped",
        json={"ItemId": EPISODE, "PositionTicks": 600_000_000},
        headers=HEADERS,
    )
    assert playback()["providers"] == {}


def test_zero_progress_on_an_unresolvable_id_is_bucketed_as_unknown() -> None:
    client.post(
        "/Sessions/Playing/Stopped",
        json={"ItemId": "g2:coldkey", "PositionTicks": 0},
        headers=HEADERS,
    )
    assert playback()["providers"][UNKNOWN_PROVIDER]["no_progress"] == 1


def test_a_zero_progress_stop_writes_no_resume_position() -> None:
    """Why this state had no owner before: the shelf drops the evidence.

    ``record_playback`` ignores zero/negative positions by design (#214) —
    there is nothing to resume TO — so the very report that witnesses
    "served but never played" is the report the resume store discards. The
    attestation rides along with the write rather than replacing it, and
    the two rules stay complementary: a stop at a real position lands on
    the shelf and is not a fault on the axis.
    """
    from cs_uk_api.catalog import recent_playback

    client.post(
        "/Sessions/Playing/Stopped",
        json={"ItemId": EPISODE, "PositionTicks": 0},
        headers=HEADERS,
    )
    assert EPISODE not in recent_playback()
    assert playback()["providers"][PROVIDER]["no_progress"] == 1

    client.post(
        "/Sessions/Playing/Stopped",
        json={"ItemId": EPISODE, "PositionTicks": 600_000_000, "RunTimeTicks": 7_000_000_000},
        headers=HEADERS,
    )
    assert EPISODE in recent_playback()
    assert playback()["providers"][PROVIDER]["no_progress"] == 1


def test_the_axes_are_independent_state() -> None:
    """Recording a refusal never touches the lane tracker (the seam's whole
    reason for owning its own state)."""
    TRACKER.reset()
    PLAY_ATTESTATION.refused(PROVIDER, EPISODE, "not_found", "native")
    assert TRACKER.status(PROVIDER) == "ok"
    assert TRACKER.last_error_at(PROVIDER) is None
    assert playback()["providers"][PROVIDER]["refused"] == 1
