"""Tests for the startup catalog warm (tickets #204/#210).

Seams under test:
- unit: ``first_card_keys`` — the first N group keys of every non-empty
  home row, in row order (a pure function over the snapshot).
- unit: ``warm_catalog`` — builds the home snapshot ONCE, then resolves
  each chosen card's detail via ``resolve_group_content``; a failed
  resolve never aborts the warm; state counters reflect what ran.
- API:  ``GET /api/health`` exposes the catalog-warm state block
  (``status`` / ``home_warmed`` / ``content_warmed``).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import catalog_warm
from cs_uk_api.main import app
from cs_uk_api.models import ContentResponse, HomeItem, HomeResponse, HomeRow, Translation

client = TestClient(app)


def _item(key: str) -> HomeItem:
    return HomeItem(
        group_key=key,
        title=key,
        year=2024,
        form="series",
    )


def _content(key: str) -> ContentResponse:
    return ContentResponse(
        id=f"p:{key}",
        title=key,
        year=2024,
        form="series",
        translations=[Translation(id="uk", label="Українська")],
    )


def _home(rows: list[list[str]]) -> HomeResponse:
    return HomeResponse(
        rows=[HomeRow(title=f"row{i}", type=f"t{i}", items=[_item(k) for k in ks])
              for i, ks in enumerate(rows)]
    )


# --------------------------------------------------------------- first_card_keys

def test_first_card_keys_takes_first_card_of_each_nonempty_row() -> None:
    home = _home([["a", "a2"], [], ["b"], ["c", "c2", "c3"]])
    assert catalog_warm.first_card_keys(home) == ["a", "b", "c"]


def test_first_card_keys_empty_rows_are_skipped() -> None:
    home = _home([[], [], []])
    assert catalog_warm.first_card_keys(home) == []


def test_first_card_keys_per_row_cap() -> None:
    home = _home([["a", "a2", "a3"], ["b"]])
    assert catalog_warm.first_card_keys(home, per_row=2) == ["a", "a2", "b"]


def test_first_card_keys_dedups_across_rows() -> None:
    # The same merged card can appear in two rows; the warm should not
    # scrape it twice.
    home = _home([["a"], ["a", "b"]])
    assert catalog_warm.first_card_keys(home, per_row=2) == ["a", "b"]


# ------------------------------------------------------------------ warm_catalog

async def test_warm_catalog_builds_home_once_and_resolves_each_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home([["gk1", "gk2"], ["gk3"]])
    calls: list[str] = []
    home_calls: list[str] = []

    async def fake_load_home() -> HomeResponse:
        home_calls.append("load_home")
        return home

    async def fake_resolve(gk: str) -> ContentResponse:
        calls.append(gk)
        return _content(gk)

    monkeypatch.setattr(catalog_warm, "load_home", fake_load_home)
    monkeypatch.setattr(catalog_warm, "resolve_group_content", fake_resolve)

    state = await catalog_warm.warm_catalog()

    assert home_calls == ["load_home"]
    assert calls == ["gk1", "gk3"]
    assert state.home_warmed is True
    assert state.content_warmed == 2
    assert state.failed == 0


async def test_warm_catalog_tolerates_resolve_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home([["gk1"], ["gk2"], ["gk3"]])

    async def fake_load_home() -> HomeResponse:
        return home

    async def flaky_resolve(gk: str) -> ContentResponse | None:
        if gk == "gk2":
            raise RuntimeError("provider exploded")
        return _content(gk)

    monkeypatch.setattr(catalog_warm, "load_home", fake_load_home)
    monkeypatch.setattr(catalog_warm, "resolve_group_content", flaky_resolve)

    state = await catalog_warm.warm_catalog()

    assert state.content_warmed == 2
    assert state.failed == 1


async def test_warm_catalog_tolerates_home_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_load_home() -> HomeResponse:
        raise RuntimeError("fan-out blew up")

    monkeypatch.setattr(catalog_warm, "load_home", broken_load_home)

    state = await catalog_warm.warm_catalog()

    assert state.home_warmed is False
    assert state.content_warmed == 0
    assert state.failed == 1
    assert state.status == "failed"


async def test_warm_catalog_none_result_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home([["gk1"], ["gk2"]])

    async def fake_load_home() -> HomeResponse:
        return home

    async def none_resolve(gk: str) -> ContentResponse | None:
        # item unavailable (cold gated / unresolvable) — not an exception
        return None

    monkeypatch.setattr(catalog_warm, "load_home", fake_load_home)
    monkeypatch.setattr(catalog_warm, "resolve_group_content", none_resolve)

    state = await catalog_warm.warm_catalog()

    assert state.content_warmed == 0
    assert state.failed == 0  # a None verdict is a legit outcome, not a failure
    assert state.status == "done"


# ---------------------------------------------------------------------- API

def test_health_exposes_catalog_warm_state() -> None:
    body = client.get("/api/health").json()
    assert "catalog_warm" in body
    block = body["catalog_warm"]
    assert set(block) >= {"status", "home_warmed", "content_warmed", "failed"}
