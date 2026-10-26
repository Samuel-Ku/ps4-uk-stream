"""The playback axis of the drift monitor (spec #285's other half).

The prober answers "does this provider still answer?" — a listing that parses
through the real adapter, a deep probe whose HEAD is 2xx. It cannot answer the
rest of the question: **did the bytes play?** A provider whose every stream is
served and then choked on by the player answers every probe the monitor has,
which is how a catalog full of unplayable titles reads as green — the blind
spot ``play_attestation`` closes on the health surface, one hop closer to the
user than a probe ever gets.

So the monitor reads that surface and judges playback with the SAME discipline
the prober follows: the first faulting sweep logs only, the second consecutive
one files a GitHub issue, and a sweep whose plays happen again closes it.

Three things are deliberate:

  - **The window is between sweeps.** The surface exposes process-lifetime
    counters, so the fact this axis needs is a DELTA — what clients did with
    this provider's streams since the last sweep. The reference counters ride
    in the state file (``ProviderState.last_play``), which is also why counters
    that went BACKWARDS, the API having restarted, are "no evidence" rather
    than a negative delta.
  - **No evidence is neither innocence nor guilt.** A provider nobody played
    from tells this axis nothing: the streak stands and nothing is filed or
    closed. Only a sweep with traffic can accuse or acquit, so a lane with
    occasional viewers still converges instead of being acquitted by silence.
  - **The fault is "every served stream failed to play", not "a stream did".**
    A stop at zero for one of fifty plays is a title (or a client hiccup). A
    stream that is STILL PLAYING reports no stop at all, so an actively watched
    provider can never be accused by this rule either.

The reader is the API's own ``/api/health`` (``playback.providers``): the
monitor is a separate process and must never share the server's memory, so the
boundary is the wire. Every failure to read it degrades to "no evidence" — an
API that is down cannot make the monitor file an issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx

from ..models import PlayAttestation, PlayFault

#: Where the API's health surface answers by default. The service unit pins
#: uvicorn to 8003 (``deploy/DEPLOY.md`` §4) and the monitor runs on that same
#: host; ``CS_UK_DRIFT_HEALTH`` relocates it (the CLI reads it).
DEFAULT_HEALTH_URL = "http://127.0.0.1:8003/api/health"

#: The snapshot key for playback requests whose id named no provider
#: (``UNKNOWN_PROVIDER`` in ``play_attestation``). Not a lane: there is nothing
#: to file an issue against, so the monitor never judges it.
UNKNOWN_PROVIDER_KEY = "unknown"

#: How many of the surface's recent zero-progress item ids the issue body
#: carries as a triage aid. The tail itself is bounded at 20.
RECENT_ITEMS_IN_BODY = 5

#: What one sweep can conclude about a provider's playback. ``clean`` and
#: ``no-progress`` are verdicts; ``no-evidence`` is the absence of one.
PlayVerdictKind = Literal["clean", "no-progress", "no-evidence"]


@dataclass
class PlayCounters:
    """One provider's playback counters, as the health surface reports them.

    ``served`` counts stream resolutions that answered with a playable stream;
    ``no_progress`` counts the client reports of a play stopped before the
    position advanced. Both are process-lifetime totals.
    """

    served: int = 0
    no_progress: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"served": self.served, "no_progress": self.no_progress}

    @classmethod
    def from_dict(cls, raw: object) -> PlayCounters:
        """Rebuild counters from persisted state; raises on any shape
        mismatch (the store's ladder degrades to a fresh state)."""
        if not isinstance(raw, dict):
            raise TypeError("play counters must be a mapping")
        return cls(
            served=int(raw.get("served", 0)),
            no_progress=int(raw.get("no_progress", 0)),
        )


@dataclass(frozen=True)
class PlayVerdict:
    """One provider's playback verdict for the window since the last sweep."""

    kind: PlayVerdictKind
    reason: str
    served_delta: int = 0
    no_progress_delta: int = 0


@dataclass
class PlaySnapshot:
    """The playback axis as one sweep reads it: counters per lane + the tail."""

    providers: dict[str, PlayCounters] = field(default_factory=dict)
    recent: list[PlayFault] = field(default_factory=list)


def play_verdict(last: PlayCounters | None, current: PlayCounters) -> PlayVerdict:
    """Pure verdict for one provider's playback window.

    Ordered as the prober's verdict is: what cannot be judged is answered
    first, so an accusation is only ever reached with evidence behind it.

    ``no-evidence`` — no previous sweep to compare against, counters that went
    backwards (the API restarted between sweeps), or no stream served at all.
    ``clean`` — streams were served and at least one of them played; a stream
    still playing counts here, as it never reports a stop. ``no-progress`` —
    every stream served in the window ended with the client reporting zero
    progress, i.e. the bytes were served and did not play.
    """
    if last is None:
        return PlayVerdict(
            "no-evidence",
            "first observation — no previous sweep to compare against",
        )
    if current.served < last.served or current.no_progress < last.no_progress:
        return PlayVerdict(
            "no-evidence",
            "the API restarted since the last sweep (its playback counters went back)",
        )
    served = current.served - last.served
    no_progress = current.no_progress - last.no_progress
    if served == 0:
        return PlayVerdict(
            "no-evidence",
            "no stream was served since the last sweep",
            no_progress_delta=no_progress,
        )
    if no_progress == 0:
        return PlayVerdict(
            "clean",
            f"{served} stream(s) served, none stopped at zero",
            served_delta=served,
        )
    if no_progress >= served:
        return PlayVerdict(
            "no-progress",
            f"every served stream stopped at zero progress "
            f"({no_progress} reports for {served} served)",
            served_delta=served,
            no_progress_delta=no_progress,
        )
    return PlayVerdict(
        "clean",
        f"{no_progress} of {served} served stream(s) stopped at zero — "
        "playback is happening",
        served_delta=served,
        no_progress_delta=no_progress,
    )


async def fetch_play_attestation(
    url: str, http: httpx.AsyncClient
) -> tuple[PlaySnapshot | None, str | None]:
    """Read the playback axis off the API's health surface.

    ``(snapshot, None)`` when the surface answered with the shape this axis
    reads, ``(None, reason)`` when it did not — a transport error, a non-200,
    or a payload that is not ``playback`` as ``/api/health`` defines it. All
    three are the same outcome for the monitor (no evidence), so nothing here
    raises: only the report carries which one it was.
    """
    try:
        response = await http.get(url)
    except Exception as e:  # noqa: BLE001 — an unreadable surface is not an error
        return None, f"{type(e).__name__}: {e}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"
    try:
        payload = response.json()
        attestation = PlayAttestation.model_validate(payload["playback"])
    except Exception as e:  # noqa: BLE001 — a moved/unknown payload is no evidence
        return None, f"unreadable payload: {type(e).__name__}: {e}"
    return (
        PlaySnapshot(
            providers={
                provider_id: PlayCounters(
                    served=entry.served, no_progress=entry.no_progress
                )
                for provider_id, entry in attestation.providers.items()
            },
            recent=list(attestation.recent),
        ),
        None,
    )


def zero_progress_items(snapshot: PlaySnapshot, provider_id: str) -> list[str]:
    """The provider's most recent zero-progress item ids, newest last.

    From the surface's bounded tail — the one place the ids the aggregates do
    not carry appear, which is what makes an issue actionable. The tail is not
    windowed, so a body built from it says where the ids came from.
    """
    ids = [
        fault.item_id
        for fault in snapshot.recent
        if fault.outcome == "no-progress" and fault.provider == provider_id
    ]
    return ids[-RECENT_ITEMS_IN_BODY:]
