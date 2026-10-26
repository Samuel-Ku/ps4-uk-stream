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
  5. Write the machine-readable report; exit non-zero when any
     provider failed.

Deliberately standalone: it imports the provider adapters and its own
drift modules only — never the API app — so a run cannot touch the
server's state. uakino is never probed (its health is the API's
browser-session heartbeat).
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
from .baseline import (
    SHAPE_RECALIBRATE_AFTER,
    BaselineStore,
    ProviderState,
    update_signature,
    verdict_for,
)
from .issues import ISSUE_TITLE, GhIssueGateway, IssueGateway
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
    """The machine-readable report of one run."""

    run_at: str
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at,
            "providers": self.providers,
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
) -> MonitorReport:
    """One full monitor run; returns the report.

    ``day_offset`` drives the deep-probe rotation (e.g. day-of-year);
    ``deep_every_n_days`` is the full-coverage period. ``store`` /
    ``gateway`` are injectable for tests; ``report_path`` defaults to
    the runtime state file (skipped when None — tests write their own
    or assert the returned report). ``http`` injects the httpx client
    the deep probe's HEAD rides on (tests pass a fake; the default is
    the shared real client).
    """
    store = store if store is not None else BaselineStore(DEFAULT_STATE_PATH)
    gateway = gateway if gateway is not None else GhIssueGateway()

    report = MonitorReport(run_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    deep_ids = rotate_deep_providers(
        plain_http_provider_ids(), day_offset, deep_every_n_days
    )
    injected = http is not None
    client = http if http is not None else get_client()

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
    existing = gateway.find_open(provider_id)
    if existing is not None:
        log.warning("drift: %s already has open issue #%s — reusing", provider_id, existing)
        return
    try:
        number = gateway.open(provider_id, body)
        log.warning("drift: filed issue #%s for %s", number, provider_id)
    except RuntimeError as e:
        log.error("drift: failed to file issue for %s: %s", provider_id, e)


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


def _maybe_close_issue(
    provider_id: str, gateway: IssueGateway, *, comment: str, close: str
) -> None:
    """Comment and close the provider's open issue, if it has one."""
    try:
        existing = gateway.find_open(provider_id)
    except RuntimeError as e:
        log.error("drift: could not look up open issue for %s: %s", provider_id, e)
        return
    if existing is None:
        return
    try:
        gateway.comment(provider_id, existing, comment)
        gateway.close(provider_id, existing, close)
        log.info("drift: closed issue #%s for %s", existing, provider_id)
    except RuntimeError as e:
        log.error("drift: could not close issue #%s for %s: %s", existing, provider_id, e)


def issue_title(provider_id: str) -> str:
    return ISSUE_TITLE.format(provider_id=provider_id)
