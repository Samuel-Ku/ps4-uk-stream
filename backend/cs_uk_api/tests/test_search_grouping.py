"""Grouped /api/search response (issue #71).

Seams under test:

  - ``GET /api/search`` now returns ``groups: list[SearchGroup]`` instead
    of a flat ``results: list[SearchResult]``. Cross-provider duplicates
    are merged server-side via ``merge_results`` (issue #52 / v3 spec §4)
    using the ``g1:`` stateless group_key contract from #69.

  - Each ``SearchGroup`` carries one canonical ``group_key`` plus the
    full ``sources`` list — the per-provider ``SearchResult`` rows that
    collapsed into it. The UI uses ``sources`` to render the merged-source
    label and to wire source-switching on the merged detail screen
    (which then hits ``/api/content/{group_key}`` from #70).

  - The ``failures`` field is preserved unchanged (ADR-0002 contract):
    a response with both groups and failures is a normal 200.

  - The /api/content/{groupKey} route from #70 round-trips a group_key
    surfaced here back into a merged detail response.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

import cs_uk_api.catalog_state as catalog_state_mod
from cs_uk_api import main as main_mod
from cs_uk_api.catalog_state import home_cache, search_cache
from cs_uk_api.main import app
from cs_uk_api.merge import item_group_key, merge_results
from cs_uk_api.models import (
    SearchGroup,
    SearchResult,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.wire_identity import project_group

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _result(
    pid: str,
    title: str,
    *,
    media_type: str = "movie",
    year: int | None = None,
    n: str = "1",
    poster: str | None = None,
) -> SearchResult:
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=cast(Any, media_type),
        title=title,
        year=year,
        poster=poster or f"https://{pid}.example/{n}.jpg",
        url=f"https://{pid}.example/{n}",
    )


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS + caches so /api/search tests don't leak
    real upstream calls into assertions (same pattern as test_home.py).
    """
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    search_cache.clear()
    home_cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        search_cache.clear()
        home_cache.clear()


@pytest.fixture(autouse=True)
def _ready_uakino_session() -> Iterator[None]:
    """Treat uakino's browser session as already ready (issue #193).

    These tests register uakino as a healthy working provider to exercise
    merge behaviour. The fan-out skip (issue #193) drops uakino from
    ``provider=all`` searches while its session has not finished warming —
    and the explicit ``?provider=uakino`` route bounded-waits on
    ``ready_event`` — so the module stubs the session main.py reads to a
    ready one. uakino's startup marker is cleared too, so a host without
    the browser binary cannot flake the merge assertions.

    The session is restored manually (not via ``monkeypatch``): this module
    is full of ``monkeypatch.setitem(PROVIDERS, ...)`` tests whose teardown
    must run before ``isolate`` re-populates the registry — taking a
    ``monkeypatch`` here reorders fixture finalization and lets
    ``monkeypatch.undo()`` delete real providers that ``isolate`` just
    restored (pytest-randomly exposed this as registry corruption).
    """
    import asyncio

    class _ReadySession:
        def __init__(self) -> None:
            self.ready_event = asyncio.Event()
            self.ready_event.set()

        async def fetch(self, path, method="GET", data=None):  # type: ignore[no-untyped-def]
            raise AssertionError("unused: uakino providers are stubbed in this module")

        async def close(self) -> None:
            pass

    saved_get_session = main_mod.get_session
    main_mod.get_session = lambda: _ReadySession()  # type: ignore[assignment]
    # The shared merged search now lives in catalog_state (ticket #106);
    # its fan-out skip reads the SAME session seam, so both bindings are
    # stubbed to the ready session.
    saved_catalog_get_session = catalog_state_mod.get_session
    catalog_state_mod.get_session = main_mod.get_session  # type: ignore[assignment]
    main_mod.TRACKER.reset()
    try:
        yield
    finally:
        main_mod.get_session = saved_get_session
        catalog_state_mod.get_session = saved_catalog_get_session


class _StubBase(BaseProvider):
    """Minimal search-only stub; content()/stream() are unused here."""

    types = ("movie",)

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _stub(pid: str, results: list[SearchResult]) -> BaseProvider:
    """Build a one-shot search stub whose ``search`` returns the given hits."""

    class _Stub(_StubBase):
        def __init__(self) -> None:
            self.id = pid
            self.name = pid.title()
            self._results = list(results)
            self.calls = 0

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            self.calls += 1
            return list(self._results)

    return _Stub()


# ---------------------------------------------------------------------------
# Pure merge projection: ensure SearchGroup carries the right fields
# ---------------------------------------------------------------------------


def test_search_group_has_group_key_title_year_type_poster_sources() -> None:
    """SearchGroup carries group_key (g1: prefixed), canonical title/year/
    type/poster from the first source, and the full sources list."""
    items = [
        _result("uakino", "Дюна", year=2021, n="1"),
        _result("eneyida", "Дюна", year=2021, n="2"),
    ]
    groups = merge_results(items)
    assert len(groups) == 1
    # The projection (spec #309) is what the route uses — the test stops
    # reproducing the rules by hand (US5).
    proj = project_group(groups[0])
    sg = SearchGroup(
        group_key=proj.key,
        title=proj.title,
        year=proj.year,
        form=proj.form,
        poster=proj.poster,
        sources=list(proj.sources),
    )
    assert sg.group_key.startswith("g2:")
    assert sg.title == "Дюна"
    assert sg.year == 2021
    assert sg.form == "movie"
    assert sg.poster == "https://uakino.example/1.jpg"
    assert len(sg.sources) == 2
    assert {s.provider for s in sg.sources} == {"uakino", "eneyida"}


def test_search_group_member_keys_includes_all_member_group_keys() -> None:
    """Issue #89: SearchGroup carries the full set of per-item group
    keys that contributed to the merged card. The canonical
    ``group_key`` is the yearful-preferred-min; the client matches a
    resume record against ANY member key, not only ``group_key``.

    The spec's canonical example: a yearless member + a yearful
    member. The yearful key wins as the canonical ``group_key``,
    but the yearless key is also reachable via ``member_keys`` so a
    client record keyed by the yearless member still matches."""
    items = [
        _result("uakino", "Дюна", year=2021, n="u1"),
        _result("eneyida", "Дюна", year=None, n="e1"),
    ]
    groups = merge_results(items)
    assert len(groups) == 1
    proj = project_group(groups[0])
    # Both per-item keys present in the member_keys set.
    assert set(proj.member_keys) == {item_group_key(items[0]), item_group_key(items[1])}
    # The canonical ``group_key`` is the yearful-preferred-min (the
    # yearful key wins on tie).
    assert proj.key == item_group_key(items[0])
    # The route should expose both keys — the projection (spec #309)
    # builds the same list this test used to reproduce by hand,
    # deduped first-seen-preserved.
    sg = SearchGroup(
        group_key=proj.key,
        title=proj.title,
        year=proj.year,
        form=proj.form,
        poster=proj.poster,
        sources=list(proj.sources),
        member_keys=list(proj.member_keys),
    )
    assert set(sg.member_keys) == {item_group_key(items[0]), item_group_key(items[1])}
    assert sg.group_key in sg.member_keys


# ---------------------------------------------------------------------------
# /api/search route — the response shape contract
# ---------------------------------------------------------------------------


def test_search_response_groups_field_replaces_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #1: /api/search response is grouped. The ``results`` field is
    gone; ``groups`` carries the merged cards."""
    uakino_stub = _stub("uakino", [_result("uakino", "Дюна", year=2021, n="u1")])
    eneyida_stub = _stub("eneyida", [_result("eneyida", "Дюна", year=2021, n="e1")])
    monkeypatch.setitem(PROVIDERS, "uakino", uakino_stub)
    monkeypatch.setitem(PROVIDERS, "eneyida", eneyida_stub)

    client = TestClient(app)
    r = client.get("/api/search?q=дюна")
    assert r.status_code == 200
    body = r.json()
    assert "groups" in body
    assert "results" not in body  # v3 contract: flat results are gone
    assert len(body["groups"]) == 1
    assert body["groups"][0]["group_key"].startswith("g2:")
    assert body["groups"][0]["title"] == "Дюна"
    assert len(body["groups"][0]["sources"]) == 2
    # Issue #89: member_keys is on the wire so the client can match
    # resume records against any member key, not only group_key.
    assert "member_keys" in body["groups"][0]
    assert body["groups"][0]["member_keys"] == [body["groups"][0]["group_key"]]
    providers_in_sources = {s["provider"] for s in body["groups"][0]["sources"]}
    assert providers_in_sources == {"uakino", "eneyida"}


def test_search_response_carries_query_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """The query echo is preserved alongside the new groups field."""
    monkeypatch.setitem(PROVIDERS, "uakino", _stub("uakino", []))
    client = TestClient(app)
    r = client.get("/api/search?q=smolivka")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "smolivka"
    assert body["groups"] == []


def test_search_groups_with_no_duplicates_one_source_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two providers, two distinct titles: each title becomes its own group
    with a single source."""
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [
            _result("uakino", "Дюна", year=2021, n="u1"),
            _result("uakino", "Смолфут", year=2018, n="u2"),
        ]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "eneyida",
        _stub("eneyida", [
            _result("eneyida", "Тато", year=2020, n="e1"),
        ]),
    )
    client = TestClient(app)
    r = client.get("/api/search?q=mix")
    body = r.json()
    assert len(body["groups"]) == 3
    titles = [g["title"] for g in body["groups"]]
    assert titles == ["Дюна", "Смолфут", "Тато"]
    for g in body["groups"]:
        assert len(g["sources"]) == 1


def test_search_group_key_matches_item_group_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #1 (groupKey on each card): the group's group_key equals the
    per-item group key for at least one of its sources. Cross-route
    guarantee so /api/content/{group_key} can resolve what /api/search
    surfaces (modulo the year-soft case — see next test for that)."""
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Дюна", year=2021, n="u1")]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "eneyida",
        _stub("eneyida", [_result("eneyida", "Дюна", year=2021, n="e1")]),
    )
    client = TestClient(app)
    body = client.get("/api/search?q=дюна").json()
    gk = body["groups"][0]["group_key"]

    # The same key works as a per-item key from each source.
    from cs_uk_api.merge import item_group_key

    uakino_item = _result("uakino", "Дюна", year=2021, n="u1")
    eneyida_item = _result("eneyida", "Дюна", year=2021, n="e1")
    # Both items map to the same key (they merge to one group with
    # matching effective year).
    assert item_group_key(uakino_item) == gk
    assert item_group_key(eneyida_item) == gk


def test_search_yearful_yearless_merge_prefers_yearful_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Year-soft merge: one provider has the year in the raw field, the
    other has no year and no year-in-title. They still merge into one
    group (issue #52, ``_years_match`` rule) and the group's
    ``group_key`` is the YEARFUL member's ``item_group_key`` — that's
    what ``merge_results`` documents and what the UI will use to drive
    ``/api/content/{group_key}``.

    Regression for M4 (code review): the yearful-preferred rule was
    previously unasserted for /api/search's grouped shape.
    """
    # uakino: raw year=2021 (effective year 2021).
    # eneyida: title has no extractable year + raw year=None
    # (effective year None) — but the alias "дюна" + type "movie"
    # match, so merge_results' year-soft rule collapses them.
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Дюна", year=2021, n="u1")]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "eneyida",
        _stub("eneyida", [_result("eneyida", "Дюна", n="e1")]),
    )
    client = TestClient(app)
    body = client.get("/api/search?q=дюна").json()
    assert len(body["groups"]) == 1
    gk = body["groups"][0]["group_key"]

    from cs_uk_api.merge import item_group_key

    yearful_item = _result("uakino", "Дюна", year=2021, n="u1")
    yearless_item = _result("eneyida", "Дюна", n="e1")
    yearful_key = item_group_key(yearful_item)
    yearless_key = item_group_key(yearless_item)

    # Sanity: the two items actually have DIFFERENT per-item keys
    # (year-soft rule considers them same TITLE, but the digest
    # includes the year field).
    assert yearful_key != yearless_key

    # The merged group uses the yearful member's key.
    assert gk == yearful_key
    assert gk != yearless_key


def test_search_all_yearless_merge_uses_lexically_min_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-yearless merge: every member's effective year is None (no
    year in title, raw year field None). The group key is the
    lexically-min item_group_key among members — there's no yearful
    member to prefer, so the rule falls back to the merge_results
    documented behaviour (min() of all members)."""
    # Both providers return "Дюна" with no year info anywhere.
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Дюна", n="u1")]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "eneyida",
        _stub("eneyida", [_result("eneyida", "Дюна", n="e1")]),
    )
    client = TestClient(app)
    body = client.get("/api/search?q=дюна").json()
    assert len(body["groups"]) == 1
    gk = body["groups"][0]["group_key"]

    from cs_uk_api.merge import item_group_key

    uakino_item = _result("uakino", "Дюна", n="u1")
    eneyida_item = _result("eneyida", "Дюна", n="e1")
    uakino_key = item_group_key(uakino_item)
    eneyida_key = item_group_key(eneyida_item)
    # With nothing to differentiate them, both items have the same
    # item_group_key (alias + type + None year all match). The group
    # key equals that key.
    assert uakino_key == eneyida_key
    assert gk == uakino_key


def test_search_groups_listed_per_group_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #4 (sources listed per group): the per-provider sources are
    enumerated on each group, ready for the UI to render the merged-source
    chip strip."""
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Дюна", year=2021, n="u1")]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "eneyida",
        _stub("eneyida", [_result("eneyida", "Дюна", year=2021, n="e1")]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "bambooua",
        _stub("bambooua", [_result("bambooua", "Дюна", year=2021, n="b1")]),
    )
    client = TestClient(app)
    body = client.get("/api/search?q=дюна").json()
    assert len(body["groups"]) == 1
    providers_in_sources = [s["provider"] for s in body["groups"][0]["sources"]]
    assert set(providers_in_sources) == {"uakino", "eneyida", "bambooua"}


def test_search_groups_preserve_order_within_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sources within a group preserve insertion order — first-seen wins
    the canonical title, and the order matters for stable UI rendering."""
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Дюна", year=2021, n="u1")]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "eneyida",
        _stub("eneyida", [_result("eneyida", "Дюна", year=2021, n="e1")]),
    )
    client = TestClient(app)
    body = client.get("/api/search?q=дюна").json()
    sources = body["groups"][0]["sources"]
    # merge_results preserves input order in the bucket.
    assert [s["provider"] for s in sources] == ["uakino", "eneyida"]


def test_search_empty_results_yields_empty_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROVIDERS, "uakino", _stub("uakino", []))
    client = TestClient(app)
    body = client.get("/api/search?q=zzz").json()
    assert body["groups"] == []
    # No failures either — provider returned [] with no exception.
    assert "failures" not in body


def test_search_groups_coexist_with_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0002 contract: a 200 response may carry BOTH groups and failures.
    Some providers returned data, others raised; both must be in the JSON."""
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Дюна", year=2021)]),
    )

    class _Boom(_StubBase):
        id = "boom"
        name = "Boom"

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            raise RuntimeError("upstream down")

    monkeypatch.setitem(PROVIDERS, "boom", _Boom())

    client = TestClient(app)
    body = client.get("/api/search?q=дюна").json()
    assert len(body["groups"]) == 1
    assert any(f["provider"] == "boom" for f in body["failures"])
    assert body["groups"][0]["title"] == "Дюна"


def test_search_single_provider_one_group_one_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """One provider, one hit: one group with one source."""
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Смолфут", year=2018)]),
    )
    client = TestClient(app)
    body = client.get("/api/search?q=смолфут").json()
    assert len(body["groups"]) == 1
    assert body["groups"][0]["sources"] == [
        {
            "id": "uakino:1",
            "provider": "uakino",
            "form": "movie",
            "title": "Смолфут",
            "year": 2018,
            "poster": "https://uakino.example/1.jpg",
            "url": "https://uakino.example/1",
        }
    ]


# ---------------------------------------------------------------------------
# Cross-route invariant: group_key from /api/search == item_group_key
# (i.e. merge.py produces the same key on both sides of the API surface).
# The /api/content/{group_key} → /api/home round-trip is covered by
# #70's tests (test_home.py::test_content_by_group_key_returns_merged_item_with_providers)
# and is out of scope here — both /api/search and /api/home feed the
# same merge core, so the cross-route consistency is implied by the
# share of merge_results / item_group_key, which is what this test pins.
# ---------------------------------------------------------------------------


def test_search_provider_filter_still_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``?provider=uakino`` filter survives the grouping change."""
    monkeypatch.setitem(
        PROVIDERS,
        "uakino",
        _stub("uakino", [_result("uakino", "Дюна", year=2021, n="u1")]),
    )
    monkeypatch.setitem(
        PROVIDERS,
        "eneyida",
        _stub("eneyida", [_result("eneyida", "Дюна", year=2021, n="e1")]),
    )
    client = TestClient(app)
    r = client.get("/api/search?q=дюна&provider=uakino")
    assert r.status_code == 200
    body = r.json()
    assert len(body["groups"]) == 1
    assert len(body["groups"][0]["sources"]) == 1
    assert body["groups"][0]["sources"][0]["provider"] == "uakino"


# ---------------------------------------------------------------------------
# H1 regression — cross-route groupKey divergence
# ---------------------------------------------------------------------------


def test_search_group_key_matches_home_group_key_on_year_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1 cross-route invariant: /api/search and /api/home MUST produce
    the same ``group_key`` for a year-soft title (yearful member +
    yearless member). The merge core is shared (merge_results), so both
    routes collapse to the yearful-preferred-min key.

    Before the H1 fix, /api/home's round_robin_dedup used the per-item
    ``item_group_key`` as the dedup key — which differs across year-soft
    members — so the two routes returned different keys for the same
    title, breaking the /api/content/{group_key} round-trip.

    The test exercises BOTH routes — fetching /api/search and /api/home
    with the same year-soft data and asserting the keys round-trip.
    """
    from cs_uk_api.catalog_state import home_cache
    from cs_uk_api.merge import item_group_key

    # Yearful + yearless scenario — the two items have DIFFERENT per-item
    # keys (the digest hashes the year field) but merge into one group.
    uakino_yearful = _result("uakino", "Дюна", year=2021, n="u1")
    eneyida_yearless = _result("eneyida", "Дюна", n="e1")
    # Sanity precondition for the H1 scenario.
    assert item_group_key(uakino_yearful) != item_group_key(eneyida_yearless)

    # Register stubs that serve BOTH routes: ``search()`` is called by
    # /api/search, ``browse("page")`` is called by /api/home's
    # «Нещодавно додані: Фільми» row (the provider declares
    # ``newest_section = "page"``).
    class _DualStub(_StubBase):
        def __init__(self, pid: str, items: list[SearchResult]) -> None:
            self.id = pid
            self.name = pid.title()
            self.newest_section = "page"  # /api/home hook
            self._items = items

        async def search(self, q, http):
            return list(self._items)

        async def browse(self, section, page, http):
            if section == "page":
                return list(self._items), False
            return [], False

    monkeypatch.setitem(PROVIDERS, "uakino", _DualStub("uakino", [uakino_yearful]))
    monkeypatch.setitem(PROVIDERS, "eneyida", _DualStub("eneyida", [eneyida_yearless]))
    home_cache.clear()

    client = TestClient(app)

    # /api/search collapses via merge_results → yearful-preferred-min key.
    search_key = client.get("/api/search?q=дюна").json()["groups"][0]["group_key"]

    # /api/home must surface the SAME key for the same year-soft pair.
    home_body = client.get("/api/home").json()
    newest_row = next(
        row for row in home_body["rows"] if row["title"] == "Нещодавно додані: Фільми"
    )
    assert len(newest_row["items"]) == 1, (
        "home must collapse the yearful + yearless pair into one row "
        "(H1 contract)"
    )
    home_key = newest_row["items"][0]["group_key"]

    # The whole point of the H1 fix: both routes return the same key.
    assert search_key == home_key
    # And it's the yearful member's key (yearful-preferred-min rule).
    assert search_key == item_group_key(uakino_yearful)
