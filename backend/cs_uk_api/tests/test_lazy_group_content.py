"""Lazy group content (issue #60, v3 spec §3.3).

``GET /api/content/{groupKey}?source=<provider>`` returns the v2 content
response of that ONE source (unchanged shape) plus a ``sources[]`` echo
in the v3 spec §3.2 shape (``[{ provider, id }, ...]``) so the UI can
render the source-switching chips on the details screen. The echo
carries each provider's content id so the chip strip can drive
source-switching without re-running ``/api/home``.

Why lazy: episode structures are non-isomorphic across providers (one
provider's season 1 episode 1 is not another provider's), so the backend
never merges at the content level. A focused source fetch = one upstream
call; switching to another source = a new request. A dead source fails
in isolation (502 + sources echo) — the screen stays up with the
other sources still listed.

Why not just hit ``/api/content/{provider:external}``: the client holds
a ``group_key`` (stateless identity, issue #69), not the provider's
content id. The provider's content id for a given group key has to be
discovered — that's what the home cache is for.

Seams under test:

  - ``?source=<provider>`` is a discriminator on the existing
    ``/api/content/{groupKey}`` route; without it, the legacy
    ``GroupContentResponse{item, providers}`` shape is preserved.
  - Sources lookup is populated as a side effect of ``/api/home``,
    keyed off the same TTL — staleness is pinned to the home cache.
  - Exactly ONE upstream ``content()`` call per request, regardless of
    how many providers surfaced the group.
  - The ``sources[]`` echo matches the v3 spec §3.2 grouped-card shape
    (the same ``(provider, id)`` pair the home listing surfaced).
  - Error semantics:
      - 400 ``unknown_source`` when ``?source=`` is not one of the
        group's providers (the provider is real, but doesn't carry this
        group).
      - 502 ``upstream_unreachable`` (with ``sources`` echo) when the
        requested provider's ``content()`` raises.
      - 404 ``not_found`` when the group key itself is unknown.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.main import (
    _home_cache,
    _home_sources_cache,
    _search_cache,
    app,
)
from cs_uk_api.models import (
    ContentResponse,
    SearchResult,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, model_b_axes

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _make_item(
    pid: str,
    title: str,
    media_type: str = "movie",
    year: int | None = None,
    n: str = "1",
) -> SearchResult:
    mb_form, mb_styles = model_b_axes(cast(Any, media_type))
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


from collections.abc import Iterator
from typing import Any, ClassVar, cast


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS + all caches so /api/content tests
    don't leak real upstream calls into assertions. Mirrors the pattern
    in test_home.py / test_search_grouping.py."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    _home_cache.clear()
    _home_sources_cache.clear()
    _search_cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        _home_cache.clear()
        _home_sources_cache.clear()
        _search_cache.clear()


def _both_pid(
    pid: str,
    search_item: SearchResult,
    content: ContentResponse | None = None,
    fail_content: BaseException | None = None,
) -> BaseProvider:
    """Two-purpose search + content provider stub.

    The home route uses ``search``/``browse`` to populate the
    sources side cache; the content route uses ``content`` per
    ``?source=``. The single stub exercises both paths so the
    route tests don't need a parallel stub for each side.

    Args:
        pid: Provider id (also used as name + URL host).
        search_item: The single SearchResult returned by search/browse.
        content: ContentResponse to return from content() (mutually
            exclusive with ``fail_content``).
        fail_content: If set, content() raises this exception.
    """

    class _Both(BaseProvider):
        id = pid
        name = pid.title()
        types = ("movie",)
        newest_section = "page"
        content_calls: ClassVar[list[str]] = []

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return [search_item]

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            if section == "page":
                return [search_item], False
            return [], False

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            self.content_calls.append(external_id)
            if fail_content is not None:
                raise fail_content
            assert content is not None
            return content.model_copy(deep=True)

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    return _Both()


def _register(stub: BaseProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROVIDERS, stub.id, stub)


def _dune_content(description: str) -> ContentResponse:
    return ContentResponse(
        id="placeholder",
        form="movie",
        title="Дюна",
        year=2021,
        description=description,
        translations=[Translation(id="uk", label="UK")],
    )


# ---------------------------------------------------------------------------
# Pure-route behaviour: ?source= discriminator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_content_by_group_key_without_source_returns_legacy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy ``GroupContentResponse{item, providers}`` shape is
    preserved when no ``?source=`` is given. The new ``?source=``
    discriminator must NOT change behaviour for the existing route.
    """
    p1 = _both_pid("p1", _make_item("p1", "Дюна", year=2021, n="p1-1"))
    _register(p1, monkeypatch)
    client = TestClient(app)
    client.get("/api/home")  # populate home + sources cache

    home = client.get("/api/home").json()
    gk = home["rows"][0]["items"][0]["group_key"]

    r = client.get(f"/api/content/{gk}")
    assert r.status_code == 200
    body = r.json()
    # Legacy shape — item + providers (no sources echo needed, the
    # legacy field already covers it).
    assert "item" in body
    assert "providers" in body
    assert body["item"]["group_key"] == gk
    assert body["providers"] == ["p1"]


@pytest.mark.unit
def test_content_by_group_key_with_source_fetches_one_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?source=p1`` fetches content from p1 only — exactly ONE upstream
    ``content()`` call, regardless of how many providers surfaced the
    group. Returns the v2 ContentResponse shape with a ``sources[]``
    echo (v3 spec §3.2 shape) so the UI can render the source-
    switching chips AND drive another source-switch without re-running
    ``/api/home``."""
    p1 = _both_pid(
        "p1", _make_item("p1", "Дюна", year=2021, n="p1-1"),
        content=_dune_content("From p1"),
    )
    p2 = _both_pid(
        "p2", _make_item("p2", "Дюна", year=2021, n="p2-1"),
        content=_dune_content("From p2"),
    )
    _register(p1, monkeypatch)
    _register(p2, monkeypatch)

    client = TestClient(app)
    client.get("/api/home")
    home = client.get("/api/home").json()
    gk = home["rows"][0]["items"][0]["group_key"]

    r = client.get(f"/api/content/{gk}?source=p1")
    assert r.status_code == 200
    body = r.json()
    # V2 ContentResponse shape — base fields populated by p1's content().
    assert body["title"] == "Дюна"
    assert body["description"] == "From p1"
    assert body["year"] == 2021
    assert body["group_key"] == gk
    # sources[] echo in v3 spec §3.2 shape: [{ provider, id }].
    sources = {(s["provider"], s["id"]) for s in body["sources"]}
    assert sources == {("p1", "p1:p1-1"), ("p2", "p2:p2-1")}
    # Exactly ONE upstream fetch — p2's content() must not be called.
    assert len(p1.content_calls) == 1
    assert len(p2.content_calls) == 0
    # Issue #157: content() receives the BARE external id, not the
    # wire-prefixed SearchResult.id.
    assert p1.content_calls[0] == "p1-1"


@pytest.mark.unit
def test_content_by_group_key_lazy_strips_provider_prefix_for_strict_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #157 regression: the lazy ``?source=`` branch must pass the
    bare external id to ``content()``. Real adapters (serialno,
    cikavaideya, coaninet, …) validate the external-id shape and reject
    a prefixed value with ``not_found: bad external_id``; the lazy route
    used to hand them the wire-prefixed ``SearchResult.id`` and 502'd.
    A strict stub mirrors that contract: prefixed ids raise, bare ids
    resolve."""
    from cs_uk_api.providers.base import ProviderError

    class _Strict(BaseProvider):
        id = "strict"
        name = "Strict"
        types = ("movie",)
        newest_section = "page"
        content_calls: ClassVar[list[str]] = []

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return [_make_item("strict", "Дюна", year=2021, n="s-1")]

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            if section == "page":
                return [_make_item("strict", "Дюна", year=2021, n="s-1")], False
            return [], False

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            self.content_calls.append(external_id)
            if ":" in external_id:
                raise ProviderError("not_found", f"bad external_id: {external_id!r}")
            return _dune_content("strict")

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    _register(_Strict(), monkeypatch)
    client = TestClient(app)
    client.get("/api/home")
    home = client.get("/api/home").json()
    gk = home["rows"][0]["items"][0]["group_key"]

    r = client.get(f"/api/content/{gk}?source=strict")
    assert r.status_code == 200
    assert r.json()["title"] == "Дюна"
    assert _Strict.content_calls == ["s-1"]


@pytest.mark.unit
def test_content_by_group_key_source_resolves_merged_member_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #161 regression: a merged row (yearful + yearless pair, issue
    #89) lists BOTH providers as chips, but the yearless provider's per-
    item group key differs from the canonical key — a per-item sources
    index hid it from ``resolve_group``, so ``?source=`` 400'd. The map
    must register the provider union under every member key."""
    p1 = _both_pid(
        "p1", _make_item("p1", "Дюна", year=None, n="p1-1"),
        content=_dune_content("From p1"),
    )
    p2 = _both_pid(
        "p2", _make_item("p2", "Дюна", year=2021, n="p2-1"),
        content=_dune_content("From p2"),
    )
    _register(p1, monkeypatch)
    _register(p2, monkeypatch)
    client = TestClient(app)
    client.get("/api/home")
    home = client.get("/api/home").json()
    item = home["rows"][0]["items"][0]
    gk = item["group_key"]
    # The merged row carries the union of providers + both member keys.
    assert set(item["providers"]) == {"p1", "p2"}
    assert len(item["member_keys"]) == 2
    # ?source= resolves BOTH chips — including the yearless member whose
    # per-item key differs from the canonical one.
    r1 = client.get(f"/api/content/{gk}?source=p1")
    r2 = client.get(f"/api/content/{gk}?source=p2")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["title"] == "Дюна"
    # A member-key lookup also resolves the full group.
    member = next(k for k in item["member_keys"] if k != gk)
    r3 = client.get(f"/api/content/{member}?source=p1")
    assert r3.status_code == 200
    assert r3.json()["title"] == "Дюна"


@pytest.mark.unit
def test_content_by_group_key_source_not_in_group_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``?source=`` that isn't one of the group's providers returns
    400 ``unknown_source``. The provider might be real, but doesn't
    carry this specific group."""
    p1 = _both_pid("p1", _make_item("p1", "Дюна", year=2021, n="p1-1"))
    _register(p1, monkeypatch)
    client = TestClient(app)
    client.get("/api/home")
    home = client.get("/api/home").json()
    gk = home["rows"][0]["items"][0]["group_key"]

    r = client.get(f"/api/content/{gk}?source=nonexistent")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unknown_source"


@pytest.mark.unit
def test_content_by_group_key_dead_source_returns_502_with_sources_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead source fails in isolation: 502 ``upstream_unreachable`` with
    the group's ``sources[]`` echo so the UI keeps the chip strip up and
    can degrade just the dead chip."""
    p1 = _both_pid(
        "p1", _make_item("p1", "Дюна", year=2021, n="p1-1"),
        fail_content=RuntimeError("upstream site down"),
    )
    p2 = _both_pid(
        "p2", _make_item("p2", "Дюна", year=2021, n="p2-1"),
        content=_dune_content(""),
    )
    _register(p1, monkeypatch)
    _register(p2, monkeypatch)
    client = TestClient(app)
    client.get("/api/home")
    home = client.get("/api/home").json()
    gk = home["rows"][0]["items"][0]["group_key"]

    r = client.get(f"/api/content/{gk}?source=p1")
    assert r.status_code == 502
    detail = r.json()["detail"]
    # Error code mirrors _upstream_guard's canonical vocabulary
    # (issue #81 / ADR-0002: upstream_unreachable for any non-timeout
    # upstream failure). The spec requires only 502 + sources echo,
    # not a specific error code.
    assert detail["error"] == "upstream_unreachable"
    # The chip strip must still see all sources (including the dead
    # one, so the UI knows to render it as failed rather than absent).
    sources = {(s["provider"], s["id"]) for s in detail["sources"]}
    assert sources == {("p1", "p1:p1-1"), ("p2", "p2:p2-1")}
    # Switching to the healthy source works without re-running /api/home.
    r2 = client.get(f"/api/content/{gk}?source=p2")
    assert r2.status_code == 200
    assert r2.json()["title"] == "Дюна"


@pytest.mark.unit
def test_content_by_group_key_unknown_group_key_returns_404() -> None:
    """A ``g1:``-prefixed key that isn't in the home cache (and the home
    cache is empty) returns 404 ``not_found`` — same as the existing
    behaviour for ``/api/content/{groupKey}`` without ``?source=``."""
    client = TestClient(app)
    r = client.get("/api/content/g1:deadbeefdeadbeef?source=anything")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


@pytest.mark.unit
def test_content_by_group_key_sources_populated_via_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/api/home`` is the side that knows each provider's per-group
    content id — the content route doesn't re-fetch listings. After
    ``/api/home``, ``/api/content/{gk}?source=`` works for every
    provider that surfaced the group; before ``/api/home``, it 404s.
    """
    p1 = _both_pid(
        "p1", _make_item("p1", "Дюна", year=2021, n="p1-1"),
        content=_dune_content(""),
    )
    p2 = _both_pid(
        "p2", _make_item("p2", "Дюна", year=2021, n="p2-1"),
        content=_dune_content(""),
    )
    _register(p1, monkeypatch)
    _register(p2, monkeypatch)
    client = TestClient(app)

    # Pre-home: sources cache is empty, so any g1: route 404s.
    r = client.get("/api/content/g1:deadbeefdeadbeef?source=p1")
    assert r.status_code == 404

    # Populate via /api/home.
    home = client.get("/api/home").json()
    gk = home["rows"][0]["items"][0]["group_key"]

    # Now both providers resolve.
    r1 = client.get(f"/api/content/{gk}?source=p1")
    r2 = client.get(f"/api/content/{gk}?source=p2")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["title"] == "Дюна"
    assert r2.json()["title"] == "Дюна"


@pytest.mark.unit
def test_content_by_group_key_idempotent_source_per_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a provider surfaces multiple listings for the same group, the
    sources cache stores ONE SearchResult per provider (first-seen wins)
    — the content route always picks the same id, so the result is
    deterministic."""
    p1 = _both_pid(
        "p1",
        _make_item("p1", "Дюна", year=2021, n="p1-1"),
        content=_dune_content(""),
    )
    # Add a second listing for the same group via a second stub class
    # would need a separate factory; for the "two listings, same group"
    # contract we patch _BothP1.search to return both. Simplest path:
    # override the search method after construction.
    async def two_hits(q, http):  # type: ignore[no-untyped-def]
        return [
            _make_item("p1", "Дюна", year=2021, n="p1-1"),
            _make_item("p1", "Дюна", year=2021, n="p1-2"),
        ]

    async def two_browse(section, page, http):  # type: ignore[no-untyped-def]
        if section == "page":
            return [
                _make_item("p1", "Дюна", year=2021, n="p1-1"),
                _make_item("p1", "Дюна", year=2021, n="p1-2"),
            ], False
        return [], False

    p1.search = two_hits  # type: ignore[method-assign]
    p1.browse = two_browse  # type: ignore[method-assign]
    _register(p1, monkeypatch)
    client = TestClient(app)
    client.get("/api/home")
    home = client.get("/api/home").json()
    gk = home["rows"][0]["items"][0]["group_key"]

    r1 = client.get(f"/api/content/{gk}?source=p1")
    r2 = client.get(f"/api/content/{gk}?source=p1")
    # Same external id both times — the cache key is the (group, source)
    # tuple, not the upstream listing order.
    assert r1.json()["id"] == r2.json()["id"]