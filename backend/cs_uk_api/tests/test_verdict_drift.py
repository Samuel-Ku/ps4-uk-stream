"""Verdict classification drift check + health-lane pins (candidate 2).

The item-vs-lane taxonomy (ADR-0002: 404 codes are client-side semantics,
not upstream health) has ONE owner: ``cs_uk_api.health``
(``ITEM_VERDICT_CODES`` / ``LANE_VERDICT_CODES`` / ``record_verdict``).

  - ``test_new_provider_error_code_requires_verdict_classification``
    walks the package source for every ``ProviderError`` /
    ``ProviderFailure`` code literal and fails when a code appears
    without a verdict class — the CI drift check: a new code must land
    in one of the two sets or the suite goes red.
  - ``test_record_verdict_classification`` pins the helper's semantics.
  - The browse/search pins prove the recorders route through it: item
    verdicts never flip lane health (the #373 leak class, generalized),
    lane failures still do.
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException

import cs_uk_api.config as config_mod
from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api._catalog_state.resolution import resolve_group_content
from cs_uk_api._catalog_state.search import merged_search
from cs_uk_api.health import (
    ITEM_VERDICT_CODES,
    LANE_VERDICT_CODES,
    TRACKER,
    is_classified,
    record_verdict,
)
from cs_uk_api.models import SearchResult
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError

_PKG = Path(__file__).resolve().parents[1]

#: ProviderError(/ProviderFailure( followed by a code literal — single
#: or multi-line, positional or ``code=`` keyword form.
_CODE_RE = re.compile(
    r"(?:ProviderError|ProviderFailure)\(\s*(?:code\s*=\s*)?[\"']([a-z_]+)[\"']",
    re.DOTALL,
)


def _raise_site_codes() -> set[str]:
    codes: set[str] = set()
    for path in _PKG.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _CODE_RE.finditer(text):
            # The ProviderError class definition itself (base.py) is the
            # only ``ProviderError(`` that is not a raise site; its next
            # literal is "Exception", which the pattern cannot match.
            codes.add(match.group(1))
    return codes


# A raw failure recording (``TRACKER.record(..., ok=False)``) bypasses
# the item-vs-lane classification owner entirely — the blind spot that
# let the group-content recorder keep flipping providers down on item
# verdicts after b686cf9. AST-based so docstrings don't false-positive
# and multi-line calls can't dodge the check.


def _raw_failure_record_sites() -> list[str]:
    """AST walk: ``TRACKER.record(p, ok=False)`` / ``(p, False)`` sites.

    Only a literal ``False`` counts — ``record(pid, ok)`` with a runtime
    boolean (the uakino heartbeat) is a genuine success/failure probe,
    not an unclassified verdict.
    """
    sites: list[str] = []
    for path in sorted(_PKG.rglob("*.py")):
        if "tests" in path.parts or path.name == "health.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "TRACKER"
            ):
                continue
            ok_kw = next(
                (kw for kw in node.keywords if kw.arg == "ok"), None
            )
            fail = (
                ok_kw is not None
                and isinstance(ok_kw.value, ast.Constant)
                and ok_kw.value.value is False
            ) or (
                len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value is False
            )
            if fail:
                sites.append(f"{path.relative_to(_PKG)}:{node.lineno}")
    return sites


def test_failure_recording_routes_through_record_verdict() -> None:
    """No raw failure recording outside health.py — the walk check.

    The code-literal drift test above cannot see a recorder that never
    mentions a code: a ``TRACKER.record(..., ok=False)`` call site skips
    ``record_verdict`` and reopens the #373 leak class invisibly. Any
    such site outside ``health.py`` (the classification owner) fails
    the suite. ``ok=True`` recording is not a classification decision
    and stays allowed.
    """
    sites = _raw_failure_record_sites()
    assert sites == [], (
        "Raw TRACKER.record(..., ok=False) call sites outside health.py: "
        f"{sites}. Item-vs-lane classification (ADR-0002) has one owner: "
        "route failure recording through health.record_verdict(provider_id, code)."
    )


def test_new_provider_error_code_requires_verdict_classification() -> None:
    """Every code literal in the source tree has a verdict class.

    Adding a new ProviderError code without classifying it (item vs
    lane) fails this test at review time, so the item-vs-lane rule can
    never silently grow an unclassified branch.
    """
    unclassified = sorted(_raise_site_codes() - (ITEM_VERDICT_CODES | LANE_VERDICT_CODES))
    assert unclassified == [], (
        "ProviderError codes without a verdict classification: "
        f"{unclassified}. Classify each in cs_uk_api/health.py "
        "(ITEM_VERDICT_CODES = item-level, LANE_VERDICT_CODES = lane)."
    )


@pytest.mark.parametrize("code", sorted(ITEM_VERDICT_CODES))
def test_item_codes_are_classified_item(code: str) -> None:
    assert is_classified(code)
    assert code not in LANE_VERDICT_CODES


@pytest.mark.parametrize("code", sorted(LANE_VERDICT_CODES))
def test_lane_codes_are_classified_lane(code: str) -> None:
    assert is_classified(code)
    assert code not in ITEM_VERDICT_CODES


def test_record_verdict_skips_item_codes_and_records_lane_and_generic() -> None:
    TRACKER.reset()

    for code in sorted(ITEM_VERDICT_CODES):
        record_verdict("p1", code)
    assert TRACKER.status("p1") == "ok"  # item verdicts are never lane faults

    # min_samples = 5; five lane/generic failures flip the provider down.
    for _ in range(5):
        record_verdict("p1", "upstream_unreachable")
    assert TRACKER.status("p1") == "down"

    TRACKER.reset()
    for _ in range(5):
        record_verdict("p1", None)  # generic (non-ProviderError) failure = lane
    assert TRACKER.status("p1") == "down"

    # An unknown code defaults to lane (records) — the drift test keeps
    # the known set complete, so "unknown" is a transient state.
    TRACKER.reset()
    for _ in range(5):
        record_verdict("p1", "some_future_code")
    assert TRACKER.status("p1") == "down"


def _item(pid: str, external: str, title: str) -> SearchResult:
    return SearchResult(
        id=f"{pid}:{external}",
        provider=pid,
        form="movie",
        styles=frozenset(),
        title=title,
        year=2021,
        url=f"https://{pid}.example/{external}",
    )


class _BrowseStub(BaseProvider):
    """Browse stub whose failure mode is selected per phase."""

    id = "snap"
    name = "Snap"
    types = ("movie",)
    newest_section = "new"

    def __init__(self) -> None:
        self.exc: Exception | None = None

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
        if self.exc is not None:
            raise self.exc
        return [_item("snap", "Фільм А", year=2021)], False


class _SearchStub(BaseProvider):
    """Search stub that raises a chosen exception from search()."""

    def __init__(self, pid: str, exc: Exception) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie",)
        self._exc = exc

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        raise self._exc

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    catalog_state.search_cache.clear()
    catalog_state.home_cache.clear()
    catalog_state.clear_playback()
    TRACKER.reset()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        catalog_state.clear_playback()
        TRACKER.reset()


def test_browse_item_verdict_never_flips_provider_down() -> None:
    """The snapshot (home rows) recorder routes through record_verdict.

    A provider whose browse() answers a deterministic item verdict
    (``not_found``) five times must leave lane health OK — the #373
    leak class, on the home-surface path. A genuine failure still flips
    the provider DOWN, so the lane gate keeps working.
    """
    stub = _BrowseStub()
    PROVIDERS["snap"] = stub

    # Phase A — item verdict ×5: health stays ok.
    stub.exc = ProviderError("not_found", "dead section")
    for _ in range(5):
        asyncio.run(_load_home())
    assert TRACKER.status("snap") == "ok"

    # Phase B — a generic failure ×5 flips the provider down.
    stub.exc = RuntimeError("upstream exploded")
    TRACKER.reset()
    for _ in range(5):
        asyncio.run(_load_home())
    assert TRACKER.status("snap") == "down"


def test_search_failure_attribution_records_lane_verdicts() -> None:
    """The search recorder keeps ADR-0002 lane semantics.

    Search failures are folded to lane codes before classification
    (``upstream_unreachable`` / ``timeout`` / ``internal``), so five
    failing searches still flip the provider down — search has no item
    verdicts (an empty list is the legitimate "no match" answer).
    """
    PROVIDERS["bad"] = _SearchStub("bad", ProviderError("not_found", "odd"))
    for _ in range(5):
        asyncio.run(merged_search("дюна"))
        catalog_state.search_cache.clear()
    assert TRACKER.status("bad") == "down"


async def _load_home() -> None:
    await catalog_state.snapshot.load_home()
    catalog_state.home_cache.clear()

# --- group-content seam pins (the audit's finding 1) -------------------


class _ContentStub(BaseProvider):
    """Content stub whose failure mode is selected per phase."""

    id = "gstub"
    name = "Gstub"
    types = ("movie",)

    def __init__(self) -> None:
        self.exc: Exception | None = None
        self.calls: list[str] = []

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        self.calls.append(external_id)
        if self.exc is not None:
            raise self.exc
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _seed_group_sources(stub: BaseProvider, external: str) -> str:
    """Seed the sources map so resolve_group_content can find the item."""
    from cs_uk_api.merge import item_group_key

    item = _item(stub.id, external, "Загублені")
    group_key = item_group_key(item)
    catalog_state.sources_cache.set(
        catalog_state._SOURCES_KEY, {group_key: {stub.id: item}}
    )
    return group_key


def test_group_content_item_verdict_never_flips_provider_down() -> None:
    """The group-content recorder routes through record_verdict.

    A provider whose content() answers a deterministic item verdict
    (``not_found`` — dead torrent, bogus id) five times through a cold
    ``g2:`` detail must leave lane health OK — the #373 leak class, on
    the group-content path (finding 1 of the consumer audit). The
    double-attempt retry is patched to one attempt so the pin runs fast.
    """
    stub = _ContentStub()
    PROVIDERS["gstub"] = stub
    group_key = _seed_group_sources(stub, "g1")

    saved_delay = catalog_state.resolution.CONTENT_RETRY_DELAY_S
    catalog_state.resolution.CONTENT_RETRY_DELAY_S = 0.0
    try:
        # Phase A — item verdict ×5: health stays ok.
        stub.exc = ProviderError("not_found", "dead torrent")
        for _ in range(5):
            catalog_state.content_cache.clear()
            assert asyncio.run(resolve_group_content(group_key)) is None
        assert TRACKER.status("gstub") == "ok"

        # Phase B — a lane failure ×5 flips the provider down.
        stub.exc = ProviderError("upstream_unreachable", "site down")
        for _ in range(5):
            catalog_state.content_cache.clear()
            assert asyncio.run(resolve_group_content(group_key)) is None
        assert TRACKER.status("gstub") == "down"
    finally:
        catalog_state.resolution.CONTENT_RETRY_DELAY_S = saved_delay


def test_search_pending_timeout_records_lane_verdict() -> None:
    """Pending search rows (the overall budget fired) record their lane code.

    The synthetic ``timeout`` ProviderFailure rows for tasks the budget
    killed must record like real timeout failures — a provider that
    hangs every search must still poison the window the watchdog reads
    (finding 3 of the consumer audit; ADR-0002's timeout semantics).
    """
    hang = _SearchStub("hanger", RuntimeError("never raised"))

    async def _hang(query, http):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)

    hang.search = _hang  # type: ignore[method-assign]
    PROVIDERS["hanger"] = hang

    saved_settings = config_mod.SETTINGS
    config_mod.SETTINGS = replace(saved_settings, search_total_timeout_s=0.1)
    try:
        # One hanging provider and no completed providers: ADR-0002's 502.
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(merged_search("дюна"))
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "search_timeout"
        # The failure was still recorded: the window moved (one sample is
        # below min_samples, so status stays ok — last_error proves it).
        assert TRACKER.last_error_at("hanger") is not None
    finally:
        config_mod.SETTINGS = saved_settings
