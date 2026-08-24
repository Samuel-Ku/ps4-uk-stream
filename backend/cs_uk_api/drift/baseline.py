"""Self-calibrating baseline + drift verdicts (spec #285, ticket #287;
persistence via the shared VersionedFileStore per spec #363).

Each healthy listing pass updates the provider's stored signature —
the card-count band, the form/style distribution, and the required
fields — so a growing catalog never false-positives (the baseline
follows the provider). Drift is the inverse: a probe that answers with
zero items, missing required fields, or a distribution that left the
calibrated band. Hard floors keep the thresholds honest: at least a
few items and non-empty titles.

Consecutive-failure counters persist in a small state file so the
issue-filing rule (two consecutive failures, ticket #288) survives
process restarts. Since spec #363 the file carries the shared
``{"version": 1, "data": ...}`` envelope written atomically by
``versioned_store`` — a corrupt, mismatched, or legacy bare-payload
file degrades to a fresh store (a first run calibrates instead of
tripping), and ``save()`` never raises. The verdict logic is pure — a
probe result and a signature in, a verdict out — so it is directly
unit-testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..versioned_store import VersionedFileStore
from .probe import ListingProbeResult

#: Hard floor: a listing with fewer cards than this is drift, whatever
#: the calibrated band says ("at least a few items", spec #285).
MIN_CARDS = 2


@dataclass
class Signature:
    """A provider's expected listing shape after healthy passes."""

    #: Card-count band as (low, high) observed on healthy passes. High
    #: is unbounded-ish: it only ever grows, so a growing catalog stays
    #: in band. Low is the floor: a listing under it (but over the hard
    #: floor) is a shrink worth a verdict.
    count_low: int
    count_high: int
    #: Form/style distribution as fractions of the last healthy listing.
    #: A distribution that drifts more than ``_DIST_EPS`` from the band
    #: trips the verdict (form/style flip — e.g. kinovezha search cards
    #: losing their kind signal, 2026-08-14).
    form_frac: dict[str, float] = field(default_factory=dict)
    style_frac: dict[str, float] = field(default_factory=dict)
    #: Required-fields expectation: True once a healthy pass confirmed
    #: titles and urls are populated. A later pass with empty titles or
    #: missing urls trips the verdict.
    fields_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "count_low": self.count_low,
            "count_high": self.count_high,
            "form_frac": self.form_frac,
            "style_frac": self.style_frac,
            "fields_ok": self.fields_ok,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Signature:
        return cls(
            count_low=int(raw["count_low"]),
            count_high=int(raw["count_high"]),
            form_frac={str(k): float(v) for k, v in raw.get("form_frac", {}).items()},
            style_frac={str(k): float(v) for k, v in raw.get("style_frac", {}).items()},
            fields_ok=bool(raw.get("fields_ok", True)),
        )


@dataclass
class Verdict:
    """One provider's drift verdict for a probe result."""

    ok: bool
    reason: str = ""


@dataclass
class ProviderState:
    """Persistent per-provider drift state (consecutive-failure counter)."""

    consecutive_failures: int = 0
    signature: Signature | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "signature": self.signature.to_dict() if self.signature else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProviderState:
        sig_raw = raw.get("signature")
        return cls(
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            signature=Signature.from_dict(sig_raw) if sig_raw else None,
        )


#: A form/style is SIGNIFICANT when it held at least this share of a
#: healthy listing — only significant members can trip the verdict when
#: they leave the band, so a never-seen form appearing (growth) never
#: trips and a 1-in-50 fluke card is absorbed.
_SIGNIFICANT_SHARE = 0.2

#: A significant form/style has LEFT the band when its current share
#: falls below this floor — it has effectively vanished from the
#: listing (kinovezha's kind-signal flip, 2026-08-14: search cards all
#: defaulted to movie, series share went 0.5 → 0.0).
_BAND_FLOOR = 0.05


class BaselineStore:
    """Load/save provider states in a small JSON file (runtime state).

    The store is deliberately file-backed: the nightly systemd run is a
    fresh process, so the consecutive-failure counter and the calibrated
    signatures must survive between runs. Since spec #363 it is a thin
    adapter over the shared ``VersionedFileStore``: the file carries the
    ``{"version": 1, "data": ...}`` envelope, writes are atomic and
    never raise, and ANY bad content — corrupt JSON, wrong version,
    legacy bare-payload map, shape-invalid entries — degrades to a
    fresh store (a first run calibrates instead of tripping;
    cold-start-safe by design).
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else None
        self._states: dict[str, ProviderState] = {}
        self._file: VersionedFileStore | None = None
        if self.path is not None:
            self._file = VersionedFileStore(
                path=str(self.path),
                supported_versions=(1,),
                encode=_encode_states,
                decode=_decode_states,
            )
            restored = self._file.load()
            if isinstance(restored, dict):
                self._states = restored

    def get(self, provider_id: str) -> ProviderState:
        return self._states.setdefault(provider_id, ProviderState())

    def save(self) -> None:
        if self._file is not None:
            self._file.save(self._states)


def _encode_states(states: object) -> object:
    """Provider states -> the JSON-serializable ``data`` value."""
    if not isinstance(states, dict):
        raise TypeError("baseline states map expected")
    return {
        pid: state.to_dict()
        for pid, state in sorted(states.items())
        if isinstance(pid, str) and isinstance(state, ProviderState)
    }


def _decode_states(data: object) -> dict[str, ProviderState]:
    """The ``data`` value -> provider states; raises on any shape
    mismatch (the module ladder degrades to a fresh store)."""
    if not isinstance(data, dict):
        raise TypeError("baseline payload must be a provider map")
    return {str(k): ProviderState.from_dict(v) for k, v in data.items()}


def _distribution(cards: list[Any], attr: str) -> dict[str, float]:
    """Fraction of ``cards`` carrying each distinct value of ``attr``."""
    counts: dict[str, int] = {}
    for card in cards:
        value = getattr(card, attr, None)
        if isinstance(value, (set, frozenset)):
            for v in sorted(value):
                counts[v] = counts.get(v, 0) + 1
        else:
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
    total = max(1, sum(counts.values()))
    return {k: v / total for k, v in sorted(counts.items())}


def _fields_ok(cards: list[Any]) -> bool:
    """Required fields present: non-empty titles and urls on every card."""
    for card in cards:
        if not getattr(card, "title", "").strip():
            return False
        if not getattr(card, "url", "").strip():
            return False
    return True


def verdict_for(
    result: ListingProbeResult, signature: Signature | None
) -> Verdict:
    """Pure drift verdict for one listing probe result.

    Order of checks: probe error → zero items → missing required fields
    → count under the hard floor → distribution left the calibrated band
    → count below the calibrated low (but above the hard floor). A None
    signature (first run) calibrates rather than trips.
    """
    if not result.ok:
        return Verdict(False, f"probe failed: {result.error or 'error'}")
    if not result.cards:
        return Verdict(False, "zero items")
    if not _fields_ok(result.cards):
        return Verdict(False, "missing required fields (empty title or url)")
    if len(result.cards) < MIN_CARDS:
        return Verdict(False, f"only {len(result.cards)} items (hard floor {MIN_CARDS})")
    if signature is None:
        return Verdict(True, "baseline (first calibration)")
    count = len(result.cards)
    # A listing below the calibrated low (the smallest healthy pass
    # ever seen) has left the count band — the band only ever shrinks
    # its low on healthy passes, so growth never trips and a real
    # shrink does. The two-consecutive-failures rule (ticket #288)
    # absorbs transient dips before anything is filed.
    if count < signature.count_low:
        return Verdict(False, f"count {count} below calibrated low {signature.count_low}")
    form_frac = _distribution(result.cards, "form")
    for key, expected in signature.form_frac.items():
        if expected < _SIGNIFICANT_SHARE:
            continue  # a minor member leaving can't trip the verdict
        actual = form_frac.get(key, 0.0)
        if actual < _BAND_FLOOR:
            return Verdict(
                False,
                f"form '{key}' left the band ({expected:.2f} → {actual:.2f})",
            )
    # Style distribution, when the provider ever had style-tagged
    # content (anime/cartoon/dorama). A provider whose healthy passes
    # were all plain live-action has an empty style band — nothing to
    # drift against — so the check is skipped for it.
    if signature.style_frac:
        style_frac = _distribution(result.cards, "styles")
        for key, expected in signature.style_frac.items():
            if expected < _SIGNIFICANT_SHARE:
                continue
            actual = style_frac.get(key, 0.0)
            if actual < _BAND_FLOOR:
                return Verdict(
                    False,
                    f"style '{key}' left the band ({expected:.2f} → {actual:.2f})",
                )
    return Verdict(True, "ok")


def update_signature(
    signature: Signature | None, result: ListingProbeResult
) -> Signature:
    """Calibrate/refresh a signature from a healthy pass.

    Count band widens (low shrinks, high grows) so growth and minor
    churn stay in band; distributions are replaced by the latest healthy
    pass (the baseline follows the provider).
    """
    count = len(result.cards)
    if signature is None:
        return Signature(
            count_low=count,
            count_high=count,
            form_frac=_distribution(result.cards, "form"),
            style_frac=_distribution(result.cards, "styles"),
            fields_ok=_fields_ok(result.cards),
        )
    return Signature(
        count_low=min(signature.count_low, count),
        count_high=max(signature.count_high, count),
        form_frac=_distribution(result.cards, "form"),
        style_frac=_distribution(result.cards, "styles"),
        fields_ok=_fields_ok(result.cards),
    )
