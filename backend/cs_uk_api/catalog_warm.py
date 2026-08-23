"""Startup catalog warm (tickets #204/#210).

A cold backend answers the app's first ``/UserViews`` and per-view
``/Items`` requests with a full provider fan-out (17-21s measured, B1)
— past the Switchfin client's own request timeout, so a real user
opening a library for the first time sees "Timeout was reached" and a
discarded screen. The runner worked around it with its own warmup
phase; this module removes the *backend* cause by warming before the
phone drives:

  1. ``load_home()`` — the shared 30-min home snapshot. Warm ⇒ the
     first ``/UserViews`` and every view's ``/Items`` grid serve from
     cache, no provider re-invocation.
  2. ``resolve_group_content(gk)`` for the first card(s) of each row —
     the single primitive the facade's detail/seasons/episodes/playback
     paths all read through (the 30-min ``content_cache``). Warm ⇒ the
     app's first card tap and the play chain find warm caches instead
     of a 15-20s cold scrape inside an 8s step window.

Both steps are best-effort: a provider failure or a cold-verdict 404
never aborts the warm or the process. State is exposed via
``/api/health`` so an operator can see whether the server was warm
before a client connected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from . import catalog
from .models import ContentResponse, HomeResponse

log = logging.getLogger(__name__)


class _ResolveContent(Protocol):
    async def __call__(self, group_key: str) -> ContentResponse | None: ...


async def load_home() -> HomeResponse:
    """The shared home snapshot build, through the catalog seam
    (``catalog.refresh_snapshot``). Module-level so the tests can patch
    this runner's view without touching the shared seam module."""
    return await catalog.refresh_snapshot()


async def resolve_group_content(group_key: str) -> ContentResponse | None:
    """One group key's content detail, through the catalog seam
    (``catalog.resolve_item``) with the typed verdict flattened back to
    the runner's answer: OK -> the content, UNAVAILABLE -> None — exactly
    the answers the implementation's resolver gives. Module-level so the
    tests can patch this runner's view without touching the shared seam."""
    return (await catalog.resolve_item(group_key)).content


@dataclass
class CatalogWarmState:
    """Observable outcome of one ``warm_catalog`` run (via /api/health)."""

    status: str  # "pending" | "warming" | "done" | "failed"
    home_warmed: bool
    content_warmed: int
    failed: int
    #: First-card group keys whose content was NOT in the cache after
    #: the warm (ticket #224) — a provider-error None (upstream down at
    #: warm time) is indistinguishable from a legit unavailable verdict
    #: here, so the failed counter alone MASKED run8's cold popular
    #: card (``content_warmed=5 failed=0`` while animeon was
    #: unreachable). The health endpoint and the runner read this to
    #: see which rows the warm actually covered.
    cold_keys: list[str] = field(default_factory=list)


def first_card_keys(home: HomeResponse, per_row: int = 1) -> list[str]:
    """The first ``per_row`` group keys of every non-empty home row.

    Row order preserved; duplicates across rows dropped (a merged card
    can surface in more than one row — scrape it once). Empty rows are
    skipped: nothing to warm.
    """
    seen: set[str] = set()
    keys: list[str] = []
    for row in home.rows:
        for item in row.items[:per_row]:
            if item.group_key not in seen:
                seen.add(item.group_key)
                keys.append(item.group_key)
    return keys


async def warm_catalog(
    per_row: int = 1,
    *,
    _resolve: _ResolveContent | None = None,
) -> CatalogWarmState:
    """Build the home snapshot, then warm the first cards' detail chain.

    One-shot best-effort: a broken home build marks the run failed and
    stops; a per-card resolve failure is counted and skipped (the card
    stays cold — the app's own retry / runner warmup still covers it).
    """
    state = CatalogWarmState(
        status="warming", home_warmed=False, content_warmed=0, failed=0,
    )
    resolve = _resolve if _resolve is not None else resolve_group_content
    try:
        home = await load_home()
    except Exception:
        log.exception("catalog warm: home build failed")
        state.status = "failed"
        state.failed = 1
        return state
    state.home_warmed = True
    for gk in first_card_keys(home, per_row=per_row):
        try:
            content = await resolve(gk)
        except Exception:  # noqa: BLE001 — keep warming the other cards
            log.warning("catalog warm: card detail failed key=%s", gk)
            state.failed += 1
            state.cold_keys.append(gk)
            continue
        if content is not None:
            # A non-None verdict means the provider's detail landed in
            # the content cache — the card is actually warm. None is a
            # legit "item unavailable" (gated/unresolvable) verdict:
            # nothing to count, nothing to blame — but the card IS
            # cold, and the health endpoint must say so (#224: run8's
            # warm masked a provider-down popular card behind failed=0).
            state.content_warmed += 1
        else:
            state.cold_keys.append(gk)
    state.status = "done"
    return state
