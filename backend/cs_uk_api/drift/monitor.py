"""Drift monitor orchestration (spec #285).

Ties the pieces together for one nightly run:

  1. Probe every plain-HTTP provider's listing (real adapters).
  2. Deep-probe the rotating subset (content → stream → HEAD).
  3. Verdict each against the self-calibrating baseline; update the
     baseline on healthy passes; bump/reset consecutive-failure
     counters; adopt the provider's current shape when a SHAPE failure
     persists (``SHAPE_RECALIBRATE_AFTER``) instead of failing forever.
  4. File/reuse/close GitHub issues per the two-consecutive-failures
     rule (ticket #288), and close the issue when the baseline
     converged on a moved shape — said in the closing comment, so an
     accepted change is never reported as a recovery.
  5. Judge the PLAYBACK axis from the API's health surface: a provider
     that SERVED streams the client never played files its own issue
     (``drift/attestation.py``) on the same second-consecutive-sweep
     rule. This is the half of the question no probe can answer — the
     bytes are served, so every listing and every HEAD is green while
     nothing plays — and it runs outside the probe loop, so a lane the
     monitor never probes (uakino, the torrent engine) is judged too.
  6. Write the machine-readable report; exit non-zero when any
     provider failed on either axis.

Deliberately standalone: it imports the provider adapters and its own
drift modules only — never the API app — so a run cannot touch the
server's state. What it needs from the API it reads over the wire, from
``/api/health`` (the playback axis). uakino is never probed (its health
is the API's browser-session heartbeat).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

import cs_uk_api.providers._registry  # noqa: F401  (bootstraps the provider registry)

from ..http_client import close_client, get_client
from ..providers import PROVIDERS
from ..versioned_store import atomic_write_text
from .attestation import (
    DEFAULT_HEALTH_URL,
    UNKNOWN_PROVIDER_KEY,
    PlaySnapshot,
    PlayVerdict,
    fetch_play_attestation,
    play_verdict,
    zero_progress_items,
)
from .baseline import (
    SHAPE_RECALIBRATE_AFTER,
    BaselineStore,
    ProviderState,
    update_signature,
    verdict_for,
)
from .issues import DriftFault, GhIssueGateway, IssueGateway
from .probe import (
    EXCLUDED_PROVIDER_IDS,
    DeepProbeResult,
    ListingProbeResult,
    probe_deep,
    probe_listing,
    rotate_deep_providers,
)

log = logging.getLogger(__name__)

#: Default runtime-state locations (gitignored, next to the poster
#: cache the API already uses). Overridable via env so tests and the
#: systemd unit can point them anywhere.
DEFAULT_STATE_PATH = os.path.expanduser("~/.cache/cs-uk-api/drift-state.json")
DEFAULT_REPORT_PATH = os.path.expanduser("~/.cache/cs-uk-api/drift-report.json")


@dataclass
class ProviderRun:
    """One provider's full nightly result (probes + verdict + state)."""

    provider_id: str
    listing: ListingProbeResult
    deep: DeepProbeResult | None = None
    verdict_ok: bool = True
    verdict_reason: str = ""
    consecutive_failures: int = 0
    signature_updated: bool = False
    #: True when this run adopted a moved shape as the new baseline (the
    #: provider was healthy all along, so this is not a failure — the
    #: report distinguishes "accepted the new shape" from "healthy").
    recalibrated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.verdict_ok,
            "reason": self.verdict_reason,
            "listing": {
                "ok": self.listing.ok,
                "kind": self.listing.kind,
                "count": len(self.listing.cards),
                "error": self.listing.error,
            },
            "deep": (
                {
                    "ok": self.deep.ok,
                    "error": self.deep.error,
                    "stream_url": self.deep.stream_url,
                    "head_status": self.deep.head_status,
                }
                if self.deep is not None
                else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "signature_updated": self.signature_updated,
            "recalibrated": self.recalibrated,
        }


@dataclass
class MonitorReport:
    """The machine-readable report of one run.

    ``providers`` is the probe axis (the providers this run swept) and
    ``playback`` the playback axis, keyed by every lane the health surface
    reported — not only the swept ones, because that axis is not probing.
    ``attestation_error`` names why the playback axis was not read, so an
    empty ``playback`` says which of the two things it means.
    """

    run_at: str
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    playback: dict[str, dict[str, Any]] = field(default_factory=dict)
    attestation_error: str | None = None
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at,
            "providers": self.providers,
            "playback": self.playback,
            "attestation_error": self.attestation_error,
            "failed": self.failed,
            "skipped": self.skipped,
        }


def plain_http_provider_ids() -> list[str]:
    """Registered providers minus the excluded ones, in registry order."""
    return [pid for pid in PROVIDERS if pid not in EXCLUDED_PROVIDER_IDS]


async def run_once(
    *,
    day_offset: int,
    deep_every_n_days: int = 6,
    store: BaselineStore | None = None,
    gateway: IssueGateway | None = None,
    report_path: str | os.PathLike[str] | None = None,
    skip_issues: bool = False,
    http: httpx.AsyncClient | None = None,
    play: PlaySnapshot | None = None,
    health_url: str | None = DEFAULT_HEALTH_URL,
) -> MonitorReport:
    """One full monitor run; returns the report.

    ``day_offset`` drives the deep-probe rotation (e.g. day-of-year);
    ``deep_every_n_days`` is the full-coverage period. ``store`` /
    ``gateway`` are injectable for tests; ``report_path`` defaults to
    the runtime state file (skipped when None — tests write their own
    or assert the returned report). ``http`` injects the httpx client
    the deep probe's HEAD and the health read ride on (tests pass a
    fake; the default is the shared real client).

    ``health_url`` is the API's health surface, read for the playback
    axis; ``play`` injects that snapshot directly (tests) and skips the
    read. Pass ``health_url=None`` to judge no playback at all — the CLI
    always passes one — and the report then says so instead of claiming a
    clean sweep nothing looked at.
    """
    store = store if store is not None else BaselineStore(DEFAULT_STATE_PATH)
    gateway = gateway if gateway is not None else GhIssueGateway()

    report = MonitorReport(run_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    deep_ids = rotate_deep_providers(
        plain_http_provider_ids(), day_offset, deep_every_n_days
    )
    injected = http is not None
    client = http if http is not None else get_client()
    play_snapshot, attestation_error = await _read_playback(play, health_url, client)

    try:
        for provider_id in plain_http_provider_ids():
            provider = PROVIDERS[provider_id]
            listing = await probe_listing(provider, client)
            state = store.get(provider_id)

            # Deep probe (rotating subset): the first listing card.
            deep: DeepProbeResult | None = None
            if provider_id in deep_ids and listing.cards:
                deep = await probe_deep(provider, client, listing.cards[0])

            # Verdict: listing verdict; a failed deep probe also fails
            # the provider (stream-level drift, e.g. animeon's lost
            # URLs — ticket #285's concrete regression case).
            verdict = verdict_for(listing, state.signature)
            deep_ok = deep is None or deep.ok
            verdict_ok = verdict.ok and deep_ok
            reason = verdict.reason
            if verdict.ok and not deep_ok and deep is not None:
                reason = f"deep probe failed: {deep.error or 'error'}"

            # Consecutive-failure bookkeeping (ticket #288): a healthy
            # pass resets the counters and refreshes the baseline; a
            # failure bumps them.
            signature_updated = False
            recalibrated = False
            if verdict_ok:
                state.consecutive_failures = 0
                state.shape_streak = 0
                new_sig = update_signature(state.signature, listing)
                signature_updated = new_sig != state.signature
                state.signature = new_sig
                if not skip_issues:
                    _maybe_close_recovered(provider_id, gateway)
            else:
                state.consecutive_failures += 1
                # Only a SHAPE failure moves the convergence clock: a
                # broken provider (or a failed deep probe) is not serving
                # at all, so there is no new shape to adopt — and an
                # unstable provider that alternates must not silently
                # recalibrate on two out of three sweeps.
                if verdict.kind == "shape" and deep_ok:
                    state.shape_streak += 1
                else:
                    state.shape_streak = 0
                if state.shape_streak >= SHAPE_RECALIBRATE_AFTER:
                    # CONVERGENCE (the #382/#383 shape): the provider
                    # is serving fine, its shape just moved, and the
                    # baseline only follows on HEALTHY passes — so
                    # without this the same stale band fails forever and
                    # the issue can never close. The third consecutive
                    # shape observation makes the new shape the baseline.
                    streak = state.shape_streak
                    state.signature = update_signature(state.signature, listing)
                    signature_updated = True
                    state.consecutive_failures = 0
                    state.shape_streak = 0
                    verdict_ok = True
                    recalibrated = True
                    reason = f"recalibrated after {streak} shape sweeps: {verdict.reason}"
                    log.warning(
                        "drift: %s recalibrated — new shape adopted after %d sweeps (%s)",
                        provider_id,
                        streak,
                        verdict.reason,
                    )
                    if not skip_issues:
                        _maybe_close_recalibrated(provider_id, gateway, streak, reason)
                elif not skip_issues:
                    _maybe_file_issue(provider_id, state, listing, gateway)

            run = ProviderRun(
                provider_id=provider_id,
                listing=listing,
                deep=deep,
                verdict_ok=verdict_ok,
                verdict_reason=reason,
                consecutive_failures=state.consecutive_failures,
                signature_updated=signature_updated,
                recalibrated=recalibrated,
            )
            report.providers[provider_id] = run.to_dict()
            if not verdict_ok:
                report.failed.append(provider_id)

        # The playback axis, after the probe loop: it judges the lanes the
        # health surface reported, which is not the set the probes cover.
        if play_snapshot is not None:
            playback, play_failed = _sweep_playback(
                play_snapshot, store, gateway, skip_issues=skip_issues
            )
            report.playback = playback
            for provider_id in play_failed:
                if provider_id not in report.failed:
                    report.failed.append(provider_id)
        report.attestation_error = attestation_error

        store.save()
        if report_path is not None:
            atomic_write_text(
                str(report_path), json.dumps(report.to_dict(), indent=2, sort_keys=True)
            )
        return report
    finally:
        # Only close the client WE created (the default shared one); an
        # injected test client belongs to the caller.
        if not injected:
            await close_client()


async def _read_playback(
    injected: PlaySnapshot | None, health_url: str | None, http: httpx.AsyncClient
) -> tuple[PlaySnapshot | None, str | None]:
    """The playback axis's input: an injected snapshot, or the health surface.

    ``health_url=None`` means the caller configured no surface, which is
    reported as such rather than passed off as a sweep with nothing to say:
    an axis nobody read must not look like an axis that found nothing.
    """
    if injected is not None:
        return injected, None
    if health_url is None:
        return None, "no health URL configured"
    return await fetch_play_attestation(health_url, http)


def _sweep_playback(
    snapshot: PlaySnapshot,
    store: BaselineStore,
    gateway: IssueGateway,
    *,
    skip_issues: bool,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Verdict every lane the health surface reported, on the playback axis.

    Deliberately outside the probe loop: this axis is not probing, so lanes
    the monitor never probes — uakino (``EXCLUDED_PROVIDER_IDS``) and the
    torrent engine (``ENGINE_TRACKER_ID``) — are judged here as well. Their
    exclusion is about not warming a second browser session; these verdicts
    come from the API's own playback records, so no probe is spent on them.

    Returns the report's ``playback`` section and the lanes that failed this
    sweep. A fault fails the sweep whether or not it has reached the filing
    threshold, exactly as a probe failure does; when it has, the lane's own
    issue is filed or reused (``drift/issues.py``, fault ``playback``).
    """
    section: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    for provider_id, counters in sorted(snapshot.providers.items()):
        if provider_id == UNKNOWN_PROVIDER_KEY:
            # Malformed ids and cold group keys share this bucket: it names no
            # lane, so there is nothing to file an issue against.
            continue
        state = store.get(provider_id)
        verdict = play_verdict(state.last_play, counters)
        if verdict.kind == "no-progress":
            state.play_failures += 1
            failed.append(provider_id)
            if not skip_issues:
                _maybe_file_play_issue(provider_id, state, verdict, snapshot, gateway)
        elif verdict.kind == "clean":
            state.play_failures = 0
            if not skip_issues:
                _maybe_close_play_recovered(provider_id, gateway)
        # "no-evidence" neither accuses nor acquits: the streak and any open
        # issue stand until a sweep with traffic says otherwise. The counters
        # are recorded either way, so the next sweep compares against a fresh
        # reference (a restart included).
        state.last_play = counters
        section[provider_id] = {
            "verdict": verdict.kind,
            "reason": verdict.reason,
            "served_delta": verdict.served_delta,
            "no_progress_delta": verdict.no_progress_delta,
            "consecutive_failures": state.play_failures,
        }
    return section, failed


def _maybe_file_issue(
    provider_id: str,
    state: ProviderState,
    listing: ListingProbeResult,
    gateway: IssueGateway,
) -> None:
    """File (or reuse) the drift issue on the SECOND consecutive failure.

    One issue per provider: ``find_open`` dedupes by title pattern, so
    repeated failures reuse the open issue instead of duplicating; the
    first failure logs only (ticket #288 AC: first failure no issue).
    A SHAPE failure that reaches ``SHAPE_RECALIBRATE_AFTER`` never gets
    here — the caller recalibrates and closes instead, so a moved shape
    files at most once and then converges.
    """
    if state.consecutive_failures < 2:
        log.warning(
            "drift: %s failing (consecutive=%d) — no issue yet",
            provider_id,
            state.consecutive_failures,
        )
        return
    body = (
        f"Drift detected for provider `{provider_id}` after "
        f"{state.consecutive_failures} consecutive failing sweeps.\n\n"
        f"Listing: {len(listing.cards)} cards (ok={listing.ok}, "
        f"error={listing.error or 'none'}).\n\n"
        "Filed automatically by the drift monitor (spec #285). The next "
        "healthy sweep will comment and close this issue."
    )
    try:
        existing = gateway.find_open(provider_id)
    except RuntimeError as e:
        # A tracker outage must not crash the sweep or lose the report (this
        # module's contract), and it must not file a duplicate either — the
        # close path already reads a failed lookup this way.
        log.error("drift: could not look up the drift issue for %s: %s", provider_id, e)
        return
    if existing is not None:
        log.warning("drift: %s already has open issue #%s — reusing", provider_id, existing)
        return
    try:
        number = gateway.open(provider_id, body)
        log.warning("drift: filed issue #%s for %s", number, provider_id)
    except RuntimeError as e:
        log.error("drift: failed to file issue for %s: %s", provider_id, e)


def _maybe_file_play_issue(
    provider_id: str,
    state: ProviderState,
    verdict: PlayVerdict,
    snapshot: PlaySnapshot,
    gateway: IssueGateway,
) -> None:
    """File (or reuse) the playback issue on the SECOND consecutive fault.

    The prober's rule on the other axis: the first faulting sweep logs only
    (ticket #288's AC), repeated faults reuse the open issue, and the issue is
    the PLAYBACK one — its own title, so a listing that recovers cannot close
    it and a provider's playback fault is never mistaken for its sweep's.
    """
    if state.play_failures < 2:
        log.warning(
            "drift: %s served streams the client never played "
            "(consecutive=%d) — no issue yet",
            provider_id,
            state.play_failures,
        )
        return
    try:
        existing = gateway.find_open(provider_id, "playback")
    except RuntimeError as e:
        log.error(
            "drift: could not look up the playback issue for %s: %s", provider_id, e
        )
        return
    if existing is not None:
        log.warning(
            "drift: %s already has open playback issue #%s — reusing",
            provider_id,
            existing,
        )
        return
    try:
        number = gateway.open(
            provider_id,
            _play_issue_body(provider_id, state, verdict, snapshot),
            "playback",
        )
        log.warning("drift: filed playback issue #%s for %s", number, provider_id)
    except RuntimeError as e:
        log.error("drift: failed to file the playback issue for %s: %s", provider_id, e)


def _play_issue_body(
    provider_id: str,
    state: ProviderState,
    verdict: PlayVerdict,
    snapshot: PlaySnapshot,
) -> str:
    """The playback issue's body: what was observed, and what it rules out.

    Written for whoever opens it cold. The counts are the window's own (the
    sweeps since the previous observation), and the ids come from the health
    surface's bounded tail — which the body says, rather than implying they
    are exactly this window's.
    """
    items = zero_progress_items(snapshot, provider_id)
    ids_line = (
        "\n\nLatest zero-progress items on the health surface's tail "
        "(bounded, not necessarily this window): " + ", ".join(f"`{i}`" for i in items) + "."
        if items
        else ""
    )
    return (
        f"Playback attestation for provider `{provider_id}`: "
        f"{state.play_failures} consecutive sweeps in which it SERVED streams "
        "the client never played.\n\n"
        f"Latest sweep: {verdict.reason}.\n\n"
        "This is the fault no probe can see: every stream resolution was "
        "answered with a playable URL, so the listing and the rotating deep "
        "probe are green while the client reports the position never "
        "advancing (`no-progress` on `/api/health`'s `playback` axis). Look at "
        "the delivery path for these items — the upstream URL, its headers, or "
        "its container/codec — not at the provider's listing."
        f"{ids_line}\n\n"
        "Filed automatically by the drift monitor (spec #285). The next sweep "
        "in which clients play this provider's streams again will comment and "
        "close this issue."
    )


def _maybe_close_recovered(provider_id: str, gateway: IssueGateway) -> None:
    """On a healthy pass, comment and close a previously open issue."""
    _maybe_close_issue(
        provider_id,
        gateway,
        comment=(
            "Provider recovered — consecutive sweeps are healthy again. "
            "Closing this drift issue."
        ),
        close="Recovered: consecutive sweeps healthy.",
    )


def _maybe_close_recalibrated(
    provider_id: str, gateway: IssueGateway, streak: int, reason: str
) -> None:
    """Close the open issue because the baseline followed the provider.

    A different statement from recovery on purpose: the provider never
    went unhealthy — it moved, and the monitor now expects the new
    shape, so the issue is closed as an accepted change rather than as
    a fault that cleared.
    """
    _maybe_close_issue(
        provider_id,
        gateway,
        comment=(
            f"Shape drift persisted for {streak} consecutive sweeps "
            f"({reason}). The provider's current shape is now its "
            "baseline, so this issue closes as an accepted change."
        ),
        close=f"Recalibrated: the current shape is the new baseline ({reason}).",
    )


def _maybe_close_play_recovered(provider_id: str, gateway: IssueGateway) -> None:
    """On a sweep whose plays happened again, comment and close the issue."""
    _maybe_close_issue(
        provider_id,
        gateway,
        fault="playback",
        comment=(
            "Playback attested again — the client played this provider's "
            "streams since the last sweep. Closing this drift issue."
        ),
        close="Recovered: the client plays this provider's streams again.",
    )


def _maybe_close_issue(
    provider_id: str,
    gateway: IssueGateway,
    *,
    fault: DriftFault = "sweep",
    comment: str,
    close: str,
) -> None:
    """Comment and close the provider's open issue for this fault, if any."""
    try:
        existing = gateway.find_open(provider_id, fault)
    except RuntimeError as e:
        log.error(
            "drift: could not look up the %s issue for %s: %s", fault, provider_id, e
        )
        return
    if existing is None:
        return
    try:
        gateway.comment(provider_id, existing, comment)
        gateway.close(provider_id, existing, close)
        log.info("drift: closed %s issue #%s for %s", fault, existing, provider_id)
    except RuntimeError as e:
        log.error(
            "drift: could not close %s issue #%s for %s: %s",
            fault,
            existing,
            provider_id,
            e,
        )
