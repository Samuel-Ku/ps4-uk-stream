"""Internal search module (spec #309 T5) — merged fan-out.

Covers ``cs_uk_api._catalog_state.search.merged_search`` directly: the
empty-query short-circuit, the shared cache hit, per-provider failure
attribution in registration order (ADR-0002), and the search-query
taste signal. The wire-level behaviour is pinned in test_search_grouping
/ test_jellyfin_search / test_failure_semantics; these tests exercise
the internal module's own contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest

from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api._catalog_state.search import merged_search
from cs_uk_api.models import SearchResult
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, model_b_axes


def _item(pid: str, external: str, title: str) -> SearchResult:
    mb_form, _mb_styles = model_b_axes(cast(Any, "movie"))
    return SearchResult(
        id=f"{pid}:{external}",
        provider=pid,
        form=mb_form,
        styles=frozenset(),
        title=title,
        year=2021,
        url=f"https://{pid}.example/{external}",
    )


class _SearchStub(BaseProvider):
    """Search-only stub with a call counter (cache-hit proof)."""

    def __init__(self, pid: str, results: list[SearchResult], *, fail: bool = False) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie",)
        self._results = list(results)
        self._fail = fail
        self.calls = 0

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._fail:
            raise RuntimeError("upstream exploded")
        return list(self._results)

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    catalog_state.search_cache.clear()
    catalog_state.clear_playback()
    catalog_state.home_cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        catalog_state.clear_playback()


def test_empty_query_short_circuits_without_provider_calls() -> None:
    stub = _SearchStub("p1", [_item("p1", "1", "Дюна")])
    PROVIDERS["p1"] = stub
    resp = asyncio.run(merged_search("   "))
    assert resp.groups == []
    assert stub.calls == 0


def test_cache_hit_reuses_fanout() -> None:
    stub = _SearchStub("p1", [_item("p1", "1", "Дюна")])
    PROVIDERS["p1"] = stub
    first = asyncio.run(merged_search("Дюна"))
    assert [g.title for g in first.groups] == ["Дюна"]
    second = asyncio.run(merged_search("Дюна"))
    assert second.groups == first.groups
    # One fan-out, two answers — the shared search cache served the hit.
    assert stub.calls == 1


def test_failure_attribution_keeps_working_provider_in_registration_order() -> None:
    good = _SearchStub("good", [_item("good", "1", "Дюна")])
    bad = _SearchStub("bad", [], fail=True)
    PROVIDERS["bad"] = bad
    PROVIDERS["good"] = good
    resp = asyncio.run(merged_search("Дюна"))
    # The good provider's results ship, ordered by registration.
    assert [g.title for g in resp.groups] == ["Дюна"]
    # The failure is attributed — never a 502 (ADR-0002).
    assert resp.failures is not None
    assert [f.provider for f in resp.failures] == ["bad"]


def test_search_records_query_as_taste_signal() -> None:
    stub = _SearchStub("p1", [_item("p1", "1", "Дюна")])
    PROVIDERS["p1"] = stub
    asyncio.run(merged_search("Дюна"))
    assert catalog_state.recent_search_queries() == ["Дюна"]
