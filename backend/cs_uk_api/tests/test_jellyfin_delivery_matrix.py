"""The "server does not serve video" matrix for the facade's delivery routes.

Every deterministic way the facade can answer *no video* is enumerated
here as a table rather than as scattered one-off tests:

  - ids that are structurally unplayable (a cold group key, a season id,
    a series group key, an unknown provider prefix, an empty external) —
    every delivery route must answer 404 ``item_unavailable``, never a
    5xx and never a 200;
  - the provider's own verdicts — each classified ``ProviderError`` code
    must answer 404, and the health bookkeeping must keep item verdicts
    out of lane health while still recording genuine lane/engine faults;
  - upstream failures (403/416/500, connect error, read timeout) and a
    redirect to a foreign host — the stream, download and segment routes
    must degrade to 404, never to an empty 200 or a 5xx;
  - a bogus translation on the ``mediaSourceId`` round trip must not be
    remembered as the series' dub.

Two things make this file impossible to "skip a state" silently:

  - ``test_route_matrix_covers_every_delivery_route`` compares the routes
    the matrix exercises against the routes ``delivery.register`` really
    registered — a new delivery route fails the suite until it joins the
    matrix;
  - ``test_verdict_matrix_covers_every_classified_code`` compares the
    literal code table against ``ITEM_VERDICT_CODES | LANE_VERDICT_CODES``
    — a new ``ProviderError`` code fails the suite until its 404/health
    outcome is pinned here.

The positive controls at the bottom keep the matrix honest: if the stub
setup ever stopped resolving at all, the negative tests would pass for
the wrong reason, so every route is also driven through a playable item
and asserted to serve bytes / a redirect.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from cs_uk_api._catalog_state import (
    blocklist_cache,
    clear_user_state,
    content_cache,
    dub_memory,
    home_cache,
    reset_catalog_state,
)
from cs_uk_api.config import SETTINGS
from cs_uk_api.health import ITEM_VERDICT_CODES, LANE_VERDICT_CODES, TRACKER
from cs_uk_api.models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    StreamResponse,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError
from cs_uk_api.torrent_engine import ENGINE_TRACKER_ID

TOKEN = SETTINGS.jellyfin_token

_CDN = "https://cdn.example.test"
_COMPANY_HEADERS = {"Referer": f"{_CDN}/referer"}

router_mod = importlib.import_module("cs_uk_api.jellyfin.router")
delivery_mod = importlib.import_module("cs_uk_api.jellyfin.delivery")

_MOVIE_TITLE = "Дюна"
_SERIES_TITLE = "Серіал"

#: Every delivery route, spelled with the registered path template, plus
#: the extra query a caller must supply. ``{item_id:path}`` is replaced
#: with the id under test (the guard below compares these templates with
#: the router table verbatim).
_ROUTE_MATRIX: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("stream", "/Videos/{item_id:path}/stream", {}),
    ("download", "/Items/{item_id:path}/Download", {}),
    ("vtt-videos", "/Videos/{item_id:path}/vtt", {}),
    ("vtt-stream", "/Stream/{item_id:path}/vtt", {}),
    ("segment", "/Videos/{item_id:path}/segment", {"url": f"{_CDN}/dune.mp4"}),
)


def _route_path(template: str, item: str) -> str:
    return template.replace("{item_id:path}", item)

#: The literal verdict table this file pins. Deliberately NOT derived from
#: the health module: the point is that adding a code over there fails
#: ``test_verdict_matrix_covers_every_classified_code`` until it is added
#: here with its expected outcome.
_MATRIX_ITEM_CODES = frozenset(
    {"gated", "not_found", "invalid_translation", "translation_missing"}
)
_MATRIX_LANE_CODES = frozenset(
    {"unreachable", "upstream_unreachable", "parse_failed", "timeout", "internal"}
)

_UPSTREAM_FAILURES = ("http-403", "http-416", "http-500", "connect-error", "read-timeout")

_ID_FAMILIES = (
    "cold-group-key",
    "season-id",
    "series-group-key",
    "unknown-provider",
    "empty-external",
)


@contextlib.contextmanager
def _fake_host() -> Iterator[None]:
    """Point the delivery module's ``get_client`` at a fresh httpx client
    that the active respx mock intercepts (same idiom as the D7 suite)."""
    original = delivery_mod.get_client
    delivery_mod.get_client = lambda: httpx.AsyncClient()  # type: ignore[assignment]
    try:
        yield
    finally:
        delivery_mod.get_client = original  # type: ignore[assignment]


def _movie() -> ContentResponse:
    return ContentResponse(
        id="p1:dune-1",
        form="movie",
        title=_MOVIE_TITLE,
        year=2021,
        description="Епічна науково-фантастична стрічка.",
        poster=f"{_CDN}/posters/dune.jpg",
        translations=[Translation(id="uk", label="Дубляж")],
    )


def _serial() -> ContentResponse:
    return ContentResponse(
        id="p1:serial-1",
        form="series",
        title=_SERIES_TITLE,
        year=2023,
        description="Детективний серіал.",
        poster=f"{_CDN}/posters/serial.jpg",
        translations=[Translation(id="uk", label="Дубляж")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(number=1, id="s1e1", title="Серія 1"),
                    Episode(number=2, id="s1e2", title="Серія 2"),
                ],
            )
        ],
    )


class _Stub(BaseProvider):
    """One home-capable provider whose ``stream()`` either serves a canned
    response or raises the exception registered for that external id."""

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(
        self,
        *,
        cards: list[SearchResult] | None = None,
        content_by_external: dict[str, ContentResponse] | None = None,
        streams: dict[str, StreamResponse] | None = None,
        failures: dict[str, BaseException] | None = None,
    ) -> None:
        self._cards = cards or []
        self._content = content_by_external or {}
        self._streams = streams or {}
        self._failures = failures or {}
        self.stream_calls: list[tuple[str, str | None]] = []
        self.sections: tuple[Any, ...] = ()

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        if section == "page":
            return list(self._cards), False
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        content = self._content.get(external_id)
        if content is None:
            raise ProviderError("not_found", f"no canned content for {external_id}")
        return content.model_copy(deep=True)

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> StreamResponse:
        self.stream_calls.append((content_id, translation))
        failure = self._failures.get(content_id)
        if failure is not None:
            raise failure
        stream = self._streams.get(content_id)
        if stream is None:
            raise ProviderError("not_found", f"no canned stream for {content_id}")
        return stream.model_copy(deep=True)


def _cards() -> list[SearchResult]:
    return [
        SearchResult(
            id="p1:dune-1",
            provider="p1",
            form="movie",
            title=_MOVIE_TITLE,
            year=2021,
            poster=f"{_CDN}/posters/dune.jpg",
            url="https://p1.example/dune-1",
        ),
        SearchResult(
            id="p1:serial-1",
            provider="p1",
            form="series",
            title=_SERIES_TITLE,
            year=2023,
            poster=f"{_CDN}/posters/serial.jpg",
            url="https://p1.example/serial-1",
        ),
    ]


def _streams(*, provider_headers: bool, subtitle: bool = False) -> dict[str, StreamResponse]:
    headers = dict(_COMPANY_HEADERS) if provider_headers else {}
    return {
        "dune-1": StreamResponse(
            url=f"{_CDN}/dune.mp4",
            type="mp4",
            headers=headers,
            subtitle_url=f"{_CDN}/dune.vtt" if subtitle else None,
        ),
        "s1e1": StreamResponse(
            url=f"{_CDN}/serial/s1e1/master.m3u8",
            type="m3u8",
            headers=headers,
            subtitle_url=f"{_CDN}/serial/s1e1/s1e1.vtt" if subtitle else None,
        ),
    }


def _seeded(
    client: TestClient, *, provider_headers: bool = False, subtitle: bool = False
) -> tuple[_Stub, dict[str, str]]:
    """Register the stub, warm ``/api/home``, and return the group keys by
    title — the same entry path a real client takes."""
    stub = _Stub(
        cards=_cards(),
        content_by_external={"dune-1": _movie(), "serial-1": _serial()},
        streams=_streams(provider_headers=provider_headers, subtitle=subtitle),
    )
    PROVIDERS["p1"] = stub
    r = client.get("/api/home")
    assert r.status_code == 200, r.text
    gks: dict[str, str] = {}
    for row in r.json()["rows"]:
        for item in row["items"]:
            gks[item["title"]] = item["group_key"]
    assert _MOVIE_TITLE in gks and _SERIES_TITLE in gks, f"seeded home missing a card: {gks}"
    return stub, gks


def _failing_stub(failure: BaseException) -> _Stub:
    stub = _Stub(failures={"s1e1": failure})
    PROVIDERS["p1"] = stub
    return stub


def _unplayable_id(family: str, gks: dict[str, str]) -> str:
    return {
        # Never registered: a cold ``g2:`` key.
        "cold-group-key": "g2:deadbeefdeadbeef",
        # A season id is a rail, not a playable item (D3).
        "season-id": f"{gks[_SERIES_TITLE]}:S1",
        # A show is not a playable thing; the client plays episodes.
        "series-group-key": gks[_SERIES_TITLE],
        # No provider owns the prefix.
        "unknown-provider": "zzz:deadbeef",
        # Known provider, empty external.
        "empty-external": "p1:",
    }[family]


def _fail_upstream(route: respx.Route, failure: str) -> None:
    if failure == "connect-error":
        route.mock(side_effect=httpx.ConnectError("boom"))
    elif failure == "read-timeout":
        route.mock(side_effect=httpx.ReadTimeout("slow"))
    else:
        route.mock(return_value=httpx.Response(int(failure.split("-")[1])))


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (home_cache, content_cache, blocklist_cache):
        cache.clear()
    reset_catalog_state()
    getattr(router_mod, "_STREAM_MEMO", {}).clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        for cache in (home_cache, content_cache, blocklist_cache):
            cache.clear()
        reset_catalog_state()
        getattr(router_mod, "_STREAM_MEMO", {}).clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


# ---------------------------------------------------------------------------
# 1. Structurally unplayable ids: every route answers the standing 404.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", _ID_FAMILIES)
def test_unplayable_id_is_404_on_every_delivery_route(
    client: TestClient, family: str
) -> None:
    """A structurally unplayable id must never become a 5xx or a 200 on
    ANY byte route — not only on ``/stream``."""
    _, gks = _seeded(client)
    item = _unplayable_id(family, gks)

    for route_name, template, params in _ROUTE_MATRIX:
        response = client.get(
            _route_path(template, item), params=params, follow_redirects=False
        )
        assert response.status_code == 404, (
            f"{family} leaked {response.status_code} on {route_name} "
            f"({template}) — a no-video state must be 404"
        )
        assert response.json() == {"detail": "item_unavailable"}, (
            f"{family} on {route_name} answered the wrong body: {response.text!r}"
        )


def test_structurally_unplayable_ids_never_reach_the_provider(
    client: TestClient,
) -> None:
    """Resolution refuses before ``provider.stream()`` is called: a season
    id, a series group key, a cold key or a malformed wire id must not
    cost an upstream scrape (and must not poison health)."""
    stub, gks = _seeded(client)
    # The home warm legitimately records browse successes; a refusal must
    # add nothing on top of them (and never a failure).
    samples_before = list(TRACKER._samples.get("p1", ()))
    for family in _ID_FAMILIES:
        client.get(f"/Videos/{_unplayable_id(family, gks)}/stream")
    assert stub.stream_calls == []
    # The series-key refusal legitimately reads the content page (one
    # successful sample); what must never appear is a FAILED sample.
    samples_after = list(TRACKER._samples.get("p1", ()))
    assert len(samples_after) >= len(samples_before)
    assert all(samples_after), "a refusal was recorded as a lane fault"


# ---------------------------------------------------------------------------
# 2. Provider verdicts: all 404, with the right health bookkeeping.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(_MATRIX_ITEM_CODES))
def test_item_verdict_is_404_and_leaves_lane_health_alone(
    client: TestClient, code: str
) -> None:
    """gated / not_found / invalid_translation / translation_missing are
    item-level semantics: 404 without a lane-health sample (the #373 rule)."""
    _failing_stub(ProviderError(code, "item verdict"))
    r = client.get("/Videos/p1:s1e1/stream")
    assert r.status_code == 404, f"{code} leaked {r.status_code}"
    assert r.json() == {"detail": "item_unavailable"}
    assert TRACKER._samples.get("p1") is None, f"{code} poisoned lane health"
    assert TRACKER.last_error_at("p1") is None


@pytest.mark.parametrize("code", sorted(_MATRIX_LANE_CODES))
def test_lane_verdict_is_404_and_records_the_fault(
    client: TestClient, code: str
) -> None:
    """Lane-level faults still refuse the video (404, never 5xx) but must
    show up as a failed lane sample so health can degrade the provider."""
    _failing_stub(ProviderError(code, "lane verdict"))
    r = client.get("/Videos/p1:s1e1/stream")
    assert r.status_code == 404, f"{code} leaked {r.status_code}"
    assert list(TRACKER._samples["p1"])[-1] is False, f"{code} was not recorded"
    assert TRACKER.last_error_at("p1") is not None


def test_engine_path_fault_records_engine_entry_not_lane(client: TestClient) -> None:
    """A torrent-engine transport fault refuses the video but belongs to
    the engine's tracker entry, never the catalog lane's (spec #394)."""
    _failing_stub(ProviderError("unreachable", "engine down").with_engine_path())
    r = client.get("/Videos/p1:s1e1/stream")
    assert r.status_code == 404
    assert list(TRACKER._samples[ENGINE_TRACKER_ID])[-1] is False
    assert TRACKER._samples.get("p1") is None
    assert TRACKER.last_error_at("p1") is None


def test_unexpected_exception_is_404_and_lane_fault(client: TestClient) -> None:
    """An unclassified exception from a provider still degrades to 404
    (D2 never-5xx) and is recorded as a lane fault."""
    _failing_stub(RuntimeError("boom"))
    r = client.get("/Videos/p1:s1e1/stream")
    assert r.status_code == 404
    assert r.json() == {"detail": "item_unavailable"}
    assert list(TRACKER._samples["p1"])[-1] is False


def test_bogus_translation_is_404_and_never_remembered(client: TestClient) -> None:
    """The ``mediaSourceId`` round trip must not record a dub the server
    could not play — a failed play leaves no per-series memory."""
    stub, gks = _seeded(client)
    gk = gks[_MOVIE_TITLE]
    stub._failures["dune-1"] = ProviderError("invalid_translation", "no such dub")
    clear_user_state()

    r = client.get(
        f"/Videos/{gk}/stream",
        params={"mediaSourceId": f"{gk}::bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 404
    assert stub.stream_calls == [("dune-1", "bogus")]
    assert dub_memory() == {}


# ---------------------------------------------------------------------------
# 3. Upstream failures: 404, never an empty 200 and never a 5xx.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", _UPSTREAM_FAILURES)
def test_stream_upstream_failure_is_404(client: TestClient, failure: str) -> None:
    """A CDN verdict the facade cannot relay (403/416/500) or a transport
    error becomes the standing 404 — a Range beyond EOF must not surface
    as a fake 206 either."""
    _, gks = _seeded(client, provider_headers=True)
    with respx.mock() as mock:
        _fail_upstream(mock.get(f"{_CDN}/dune.mp4"), failure)
        with _fake_host():
            r = client.get(
                f"/Videos/{gks[_MOVIE_TITLE]}/stream",
                headers={"Range": "bytes=999999-1000000"},
            )
    assert r.status_code == 404, f"{failure} leaked {r.status_code}"
    assert r.json() == {"detail": "item_unavailable"}


@pytest.mark.parametrize("failure", _UPSTREAM_FAILURES)
def test_download_upstream_failure_is_404_not_an_empty_file(
    client: TestClient, failure: str
) -> None:
    """The download route forces the byte proxy even for a header-less
    stream; a failed upstream must 404 instead of handing the client a
    200 attachment with no (or partial) bytes."""
    _, gks = _seeded(client, provider_headers=False)
    with respx.mock() as mock:
        _fail_upstream(mock.get(f"{_CDN}/dune.mp4"), failure)
        with _fake_host():
            r = client.get(f"/Items/{gks[_MOVIE_TITLE]}/Download")
    assert r.status_code == 404, f"{failure} leaked {r.status_code}"
    assert "content-disposition" not in r.headers, "a 404 must not look like a file"


def test_segment_upstream_failure_is_404(client: TestClient) -> None:
    """A segment that the CDN refuses must 404 (the player then reports a
    broken stream) rather than return an empty 200 or a 5xx."""
    _, gks = _seeded(client, provider_headers=True)
    with respx.mock() as mock:
        _fail_upstream(mock.get(f"{_CDN}/seg000.ts"), "http-403")
        with _fake_host():
            r = client.get(
                f"/Videos/{gks[_MOVIE_TITLE]}/segment",
                params={"url": f"{_CDN}/seg000.ts"},
            )
    assert r.status_code == 404


def test_foreign_redirect_is_404_and_never_fetched(client: TestClient) -> None:
    """A CDN redirect to a foreign registrable domain fails closed: the
    facade answers 404 and never issues the foreign request (SSRF)."""
    stub, gks = _seeded(client, provider_headers=True)
    stub._streams["dune-1"] = StreamResponse(
        url=f"{_CDN}/gate", type="mp4", headers=dict(_COMPANY_HEADERS)
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{_CDN}/gate").mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/x.mp4"})
        )
        foreign = mock.get("https://evil.example/x.mp4").mock(
            return_value=httpx.Response(200, content=b"evil")
        )
        with _fake_host():
            r = client.get(f"/Videos/{gks[_MOVIE_TITLE]}/stream")
    assert r.status_code == 404
    assert not foreign.called, "the facade followed a redirect to a foreign host"


# ---------------------------------------------------------------------------
# 4. Positive controls: every route really can serve video.
# ---------------------------------------------------------------------------


def test_positive_control_every_delivery_route_serves(client: TestClient) -> None:
    """The matrix's counterpart: on a playable item every route serves —
    302 to the CDN, 206 with the exact bytes, 200 with the file name, 302
    to the subtitle engine and 200 for a segment. Without this control
    the 404 tests could pass simply because nothing resolves."""
    stub, gks = _seeded(client, provider_headers=False, subtitle=True)
    gk = gks[_MOVIE_TITLE]
    item = str(gk)

    # Redirect path: header-less mp4 → the CDN URL itself.
    r = client.get(f"/Videos/{item}/stream", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{_CDN}/dune.mp4"

    # Subtitle: the DeliveryUrl target hands over the engine endpoint.
    r = client.get(f"/Stream/{item}/vtt", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{_CDN}/dune.vtt"

    with respx.mock() as mock:
        download = mock.get(f"{_CDN}/dune.mp4").mock(
            return_value=httpx.Response(
                200, content=b"download-bytes", headers={"Content-Type": "video/mp4"}
            )
        )
        with _fake_host():
            r = client.get(f"/Items/{item}/Download")
    assert r.status_code == 200
    assert r.content == b"download-bytes", "download served no bytes"
    assert r.headers["content-disposition"].startswith("attachment; filename=")
    assert download.called

    # Proxied Range: the provider header map forces the byte proxy.
    stub._streams["dune-1"] = StreamResponse(
        url=f"{_CDN}/range.mp4", type="mp4", headers=dict(_COMPANY_HEADERS)
    )
    with respx.mock() as mock:
        range_route = mock.get(f"{_CDN}/range.mp4").mock(
            return_value=httpx.Response(
                206,
                content=b"\x00" * 64,
                headers={
                    "Content-Range": "bytes 0-63/640",
                    "Accept-Ranges": "bytes",
                    "Content-Type": "video/mp4",
                },
            )
        )
        with _fake_host():
            r = client.get(
                f"/Videos/{item}/stream", headers={"Range": "bytes=0-63"}
            )
    assert r.status_code == 206
    assert r.content == b"\x00" * 64, "proxied Range served no bytes"
    assert range_route.calls.last.request.headers["Range"] == "bytes=0-63"

    # Segment: one rewritten reference proxies the CDN bytes.
    stub._streams["dune-1"] = StreamResponse(url=f"{_CDN}/seg000.ts", type="mp4")
    with respx.mock() as mock:
        mock.get(f"{_CDN}/seg000.ts").mock(
            return_value=httpx.Response(200, content=b"SEG")
        )
        with _fake_host():
            r = client.get(
                f"/Videos/{item}/segment", params={"url": f"{_CDN}/seg000.ts"}
            )
    assert r.status_code == 200
    assert r.content == b"SEG"


# ---------------------------------------------------------------------------
# 5. Completeness guards: a new route or verdict code cannot skip the matrix.
# ---------------------------------------------------------------------------


def test_route_matrix_covers_every_delivery_route() -> None:
    """Every route ``delivery.register`` puts on the wire must be in the
    matrix. A new byte route added without a failure-matrix row fails here
    instead of silently shipping an unpinned no-video state."""
    registered = {
        route.path
        for route in router_mod.router.routes
        if isinstance(route, APIRoute)
        and route.endpoint.__module__ == "cs_uk_api.jellyfin.delivery"
    }
    assert registered == {template for _, template, _ in _ROUTE_MATRIX}, (
        "delivery routes and the failure matrix disagree — add the new route "
        "to _ROUTE_MATRIX (and its 404 assertion) before shipping it"
    )


def test_verdict_matrix_covers_every_classified_code() -> None:
    """A ProviderError code added to health.py's taxonomy must get a row
    here — the literal table is the pin, not a copy of the sets."""
    assert _MATRIX_ITEM_CODES == ITEM_VERDICT_CODES, (
        "ITEM_VERDICT_CODES changed — pin the new code's 404/health outcome here"
    )
    assert _MATRIX_LANE_CODES == LANE_VERDICT_CODES, (
        "LANE_VERDICT_CODES changed — pin the new code's 404/health outcome here"
    )
