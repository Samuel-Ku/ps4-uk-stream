"""Play attestation: what the server did with every playback request.

``health.HealthTracker`` answers "is this lane sick?" — upstream operation
outcomes, sliding window, provider status. This module answers the other
half of the question: **did a play request get video?** Until now that
answer existed only as log lines (``jellyfin playback item verdict=…``),
so a catalog full of titles that refuse to play looked exactly like a
healthy one on every surface the backend exposes.

Three outcomes, recorded where each one happens:

    served       a stream resolution returned a playable ``StreamResponse``
    refused      it returned nothing — recorded WITH its verdict code and
                 the surface (route family) that asked
    no-progress  the client reported stopping a play whose position never
                 advanced: the bytes were served and did not play

Deliberately SEPARATE state from ``health.HealthTracker``, and this is the
point of the seam rather than an accident of layout: the refusal codes
include the item-level verdicts (``gated`` / ``not_found`` /
``invalid_translation`` / ``translation_missing``) that ADR-0002 keeps out
of lane health — the ``#373`` finding, because five dead-title clicks must
not starve a whole lane's rows. Recording a refusal here never touches the
tracker: a lane can be ``ok`` while every play request against one title
is refused, and the health surface now shows both facts side by side.

In-memory, bounded, no persistence — like the tracker, a restart is a
clean slate (the resume/user-state stores own what must survive).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import (
    PlayAttestation,
    PlayFault,
    PlayProviderAttestation,
    PlaySurface,
)

#: Bucket key for a request whose id named no resolvable provider (a
#: malformed id, a cold ``gN:`` group key). A real provider id, never
#: registered by accident: the registry's ids are the providers' own.
UNKNOWN_PROVIDER = "unknown"

#: A ``PlaybackStopInfo`` whose position is below this many ticks (one
#: second at Jellyfin's 10 000 000 ticks/s) counts as never having played.
#: Not an exact zero: a client that fails to decode reports a fraction of
#: a second, and the state to catch is "the client gave up", not "the
#: position was bit-exactly zero".
ZERO_PROGRESS_TICKS = 10_000_000

#: How many faults the ``recent`` tail keeps. Small on purpose: this is a
#: triage aid (which ids, in what order), not a log.
RECENT_MAX = 20


@dataclass
class _Bucket:
    """One provider's counters."""

    served: int = 0
    refused: int = 0
    refused_codes: dict[str, int] = field(default_factory=dict)
    no_progress: int = 0


class PlayAttestationTracker:
    """The playback-outcome state, bounded and in-memory.

    Every recorder takes the provider as it is KNOWN at the seam (None
    when the id names none) and maps it to :data:`UNKNOWN_PROVIDER`, so the
    snapshot's keys are always provider ids or that one literal.
    """

    def __init__(self, recent_max: int = RECENT_MAX) -> None:
        self._recent: deque[PlayFault] = deque(maxlen=recent_max)
        self._buckets: dict[str, _Bucket] = {}

    def served(self, provider_id: str | None) -> None:
        """A stream resolution answered with a playable stream."""
        self._bucket(provider_id).served += 1

    def refused(
        self,
        provider_id: str | None,
        item_id: str,
        code: str | None,
        surface: PlaySurface,
    ) -> None:
        """A stream resolution refused — recorded with its verdict code.

        ``code`` is the refusal's verdict as the surface named it: a
        ``ProviderError`` code, the guard's ``upstream_unreachable``
        envelope, or None when a non-``ProviderError`` crashed the call.
        """
        bucket = self._bucket(provider_id)
        bucket.refused += 1
        key = code or "unknown"
        bucket.refused_codes[key] = bucket.refused_codes.get(key, 0) + 1
        self._recent.append(
            PlayFault(
                at=_now(),
                outcome="refused",
                provider=bucket_key(provider_id),
                item_id=item_id,
                code=code,
                surface=surface,
            )
        )

    def no_progress(self, provider_id: str | None, item_id: str) -> None:
        """The client stopped a play whose position never advanced."""
        self._bucket(provider_id).no_progress += 1
        self._recent.append(
            PlayFault(
                at=_now(),
                outcome="no-progress",
                provider=bucket_key(provider_id),
                item_id=item_id,
            )
        )

    def snapshot(self) -> PlayAttestation:
        """The typed view ``/api/health`` exposes (oldest fault first)."""
        return PlayAttestation(
            providers={
                provider_id: PlayProviderAttestation(
                    served=bucket.served,
                    refused=bucket.refused,
                    refused_codes=dict(bucket.refused_codes),
                    no_progress=bucket.no_progress,
                )
                for provider_id, bucket in sorted(self._buckets.items())
            },
            recent=list(self._recent),
        )

    def reset(self) -> None:
        self._recent.clear()
        self._buckets.clear()

    def _bucket(self, provider_id: str | None) -> _Bucket:
        return self._buckets.setdefault(bucket_key(provider_id), _Bucket())


def bucket_key(provider_id: str | None) -> str:
    """The snapshot key for a provider id that may be unknown."""
    return provider_id or UNKNOWN_PROVIDER


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


#: The one instance the seams record into and the health route reads.
PLAY_ATTESTATION = PlayAttestationTracker()
