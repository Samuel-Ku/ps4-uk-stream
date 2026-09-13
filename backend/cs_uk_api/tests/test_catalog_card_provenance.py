"""Which card answers for a provider BOTH layers carry (ADR-0010, finding 4).

The settled rule: a registration carries the search layer's **own union** — the
providers the search returned *and their cards* — and snapshot precedence is a
property of a **live snapshot**, not of the record. For a provider both layers
carry that means the snapshot's card answers while the snapshot exists, and
once a replacement drops that provider the card the search returned answers.

Why this module exists beside the unit pin: the rule's consequence is a WIRE
one — *which upstream listing the detail/playable path fetches* — and a unit
pin on ``group_resolution`` cannot see it. So the scenario is driven through
the real request paths:

  - ``GET /api/content/{gk}?source=<p>`` — the native source-switch: the
    ``sources[]`` echo carries every source's id, and the external id handed
    to the provider's ``content()`` is derived from the resolved card.
  - ``GET /Items/{gk}`` — the Jellyfin facade, whose ``resolve_group_content``
    asks the first-seen card's provider for that listing's detail.

The live-snapshot step holds under either provenance (the snapshot wins while
it exists); the post-replacement steps do NOT — with the superseded "cards
from the map" provenance the echo and the upstream fetch stay on the dead
``SNAPSHOT`` listing, and both tests below fail.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_uk_api._catalog_state import (
    blocklist_cache,
    content_cache,
    home_cache,
    reset_catalog_state,
    search_cache,
)
from cs_uk_api._catalog_state.snapshot import _cache_home
from cs_uk_api.config import SETTINGS
from cs_uk_api.main import app
from cs_uk_api.models import ContentResponse, SearchResult, Translation
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

pytestmark = pytest.mark.integration

TOKEN = {"X-Emby-Token": SETTINGS.jellyfin_token}
TITLE = "Дюна"


def _card(tag: str, *, title: str = TITLE, year: int | None = 2021, pid: str = "p1") -> SearchResult:
    """One listing of the work: same title/form/year keys the same group."""
    return SearchResult(
        id=f"{pid}:{tag}",
        provider=pid,
        form="movie",
        title=title,
        year=year,
        poster=f"https://{pid}.example/{tag}.jpg",
        url=f"https://{pid}.example/{tag}",
    )


class _ProvenanceStub(BaseProvider):
    """A provider whose single card is swappable, recording every extra id.

    The recorder is the point: it says WHICH listing the detail/playable path
    asked this provider for, which is the settled rule's user-visible
    consequence.
    """

    id = "p1"
    name = "P1"
    types = ("movie",)
    newest_section = "page"

    def __init__(self) -> None:
        self.card = _card("SNAPSHOT")
        self.content_calls: list[str] = []

    async def search(self, q: str, http: Any) -> list[SearchResult]:
        return [self.card]

    async def browse(self, section: str, page: int, http: Any) -> tuple[list[SearchResult], bool]:
        return ([self.card], False) if section == "page" else ([], False)

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        self.content_calls.append(external_id)
        return ContentResponse(
            id=f"p1:{external_id}",
            form="movie",
            title=TITLE,
            year=2021,
            description=f"upstream-{external_id}",
            translations=[Translation(id="uk", label="UK")],
        )

    async def stream(self, content_id: str, translation: str | None, http: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS and every cache these routes read."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (home_cache, content_cache, blocklist_cache, search_cache):
        cache.clear()
    reset_catalog_state()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        for cache in (home_cache, content_cache, blocklist_cache, search_cache):
            cache.clear()
        reset_catalog_state()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _seed_home(client: TestClient, stub: _ProvenanceStub) -> str:
    """Register the stub and build the snapshot through the real home route."""
    PROVIDERS[stub.id] = stub
    r = client.get("/api/home")
    assert r.status_code == 200
    return str(r.json()["rows"][0]["items"][0]["group_key"])


def _source_switch(client: TestClient, gk: str, provider: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Native ``?source=``: the sources echo, and the listing actually fetched."""
    stub: _ProvenanceStub = PROVIDERS[provider]  # type: ignore[assignment]
    stub.content_calls.clear()
    r = client.get(f"/api/content/{gk}", params={"source": provider})
    assert r.status_code == 200, r.text
    echo = [(s["provider"], s["id"]) for s in r.json()["sources"]]
    return echo, list(stub.content_calls)


def _facade(client: TestClient, gk: str) -> dict[str, Any]:
    """Jellyfin ``/Items/{id}`` — its Overview is the fetched listing's payload."""
    r = client.get(f"/Items/{gk}", headers=TOKEN)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _replacement(card: SearchResult) -> None:
    """Run a replacement: ADR-0010's ONE apply step, as a rebuild calls it."""
    _cache_home({card.provider: [card]}, {}, {})


def test_the_searchs_card_answers_once_the_snapshot_drops_the_provider(client: TestClient) -> None:
    """Provider dropped, key kept by another provider.

    Step 1 the snapshot's listing answers; step 2 a search registers the SAME
    provider with a different listing and the live snapshot still wins; step 3
    a replacement carries the key under p9 only, so the search's own listing is
    what the client's detail path must fetch.
    """
    stub = _ProvenanceStub()
    gk = _seed_home(client, stub)

    # 1. The snapshot is live: its listing answers, on both surfaces.
    assert _source_switch(client, gk, "p1") == ([("p1", "p1:SNAPSHOT")], ["SNAPSHOT"])
    assert _facade(client, gk)["Overview"] == "upstream-SNAPSHOT"

    # 2. A search returns the same work at p1 with a DIFFERENT listing.
    stub.card = _card("SEARCH")
    r = client.get("/api/search", params={"q": "dune"})
    assert r.status_code == 200
    assert gk in [g["group_key"] for g in r.json()["groups"]]

    # The snapshot is still live, so it still wins (precedence, not staleness).
    assert _source_switch(client, gk, "p1") == ([("p1", "p1:SNAPSHOT")], ["SNAPSHOT"])

    # 3. A replacement lands whose snapshot carries the key under p9 — p1 is gone.
    stub9 = _ProvenanceStub()
    stub9.id = "p9"
    stub9.card = _card("p9", pid="p9")
    PROVIDERS["p9"] = stub9
    _replacement(stub9.card)

    # The key is still a snapshot row, so the union is p9 first (the live
    # snapshot's own provider) and then the registration's p1.
    echo, fetched = _source_switch(client, gk, "p1")
    assert dict(echo)["p1"] == "p1:SEARCH", "the search's own card must answer for p1"
    assert fetched == ["SEARCH"], "the detail path must fetch the search's own listing"
    # The facade asks the FIRST-SEEN provider, which is the live snapshot's own
    # p9 — a registration may never displace it. (This step holds under either
    # provenance; it pins the precedence half of the rule.)
    assert _facade(client, gk)["Overview"] == "upstream-p9"


def test_the_searchs_card_answers_once_the_snapshot_drops_the_key(client: TestClient) -> None:
    """Whole key dropped: only the registration carries the provider.

    The shape the audit probed: the replacement's snapshot does not carry the
    group key at all, so the registration is the only thing left holding p1 —
    and the listing that answers is the one the search returned, not the one a
    snapshot that no longer exists chose.
    """
    stub = _ProvenanceStub()
    gk = _seed_home(client, stub)

    stub.card = _card("SEARCH")
    assert client.get("/api/search", params={"q": "dune"}).status_code == 200

    # A rebuild that no longer carries the work at all.
    _replacement(_card("other", title="Інший Фільм", year=2020, pid="p9"))

    echo, fetched = _source_switch(client, gk, "p1")
    assert echo == [("p1", "p1:SEARCH")]
    assert fetched == ["SEARCH"]

    # ...and the facade serves the search's listing, not the dead snapshot's.
    assert _facade(client, gk)["Overview"] == "upstream-SEARCH"
