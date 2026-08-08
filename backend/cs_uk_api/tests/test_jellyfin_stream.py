"""Jellyfin conditional stream handler (ticket #107, spec D7).

D7 — the center of the spec:

  - ``GET /Videos/{id}/stream``:
      * ``StreamResponse.headers`` empty → **302 Found** to the CDN URL
        (no byte proxying);
      * otherwise → **full byte proxy** through the backend, adding the
        provider's header map to the upstream CDN request:
          - file streams: forward the client ``Range``, return the
            upstream's ``206`` / ``Content-Range`` / ``Accept-Ranges``;
          - HLS (``.m3u8``): fetch the manifest with the provider
            headers, rewrite every segment / ``URI=`` reference to the
            backend ``/Videos/{id}/segment`` route, so the client's
            segment requests carry the provider headers through the
            proxy too;
          - preserve ``Content-Type`` (``video/mp4`` /
            ``application/vnd.apple.mpegurl``).
  - ``GET /Videos/{id}/segment?url=...``: proxy a rewritten segment with
    the same provider headers.
  - Only the CDN host the provider selected for the item is reachable
    (dot-boundary; subdomains allowed) — a client pointing the segment
    route at an arbitrary host fails closed to 404 (SSRF posture, D2).
  - Cold / series / unknown ids and upstream fetch failures all degrade
    to 404 (D2 "item unavailable"); both routes sit behind
    ``require_token`` (D4).

Upstream CDN traffic is mocked with ``respx``. The byte proxy uses the
shared ``get_client()`` transport; ``_fake_host`` points that binding at
a fresh ``httpx.AsyncClient`` constructed inside the active ``respx``
mock so the CDN hops are intercepted without touching the app-wide
singleton.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import quote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.main import _blocklist_cache, _content_cache, _home_cache, _home_sources_cache
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

TOKEN = SETTINGS.jellyfin_token

_CDN = "https://cdn.example.test"
_COMPANY_HEADERS = {"Referer": f"{_CDN}/referer"}
_ENCODED = quote

router_mod = importlib.import_module("cs_uk_api.jellyfin.router")


@contextlib.contextmanager
def _fake_host() -> Iterator[None]:
    """Point the router's ``get_client`` binding at a fresh httpx client,
    which (respx being active) is intercepted for CDN hops."""
    original = router_mod.get_client
    router_mod.get_client = lambda: httpx.AsyncClient()  # type: ignore[assignment]
    try:
        yield
    finally:
        router_mod.get_client = original  # type: ignore[assignment]


def _dune() -> ContentResponse:
    return ContentResponse(
        id="p1:dune-1",
        type="movie",
        title="Дюна",
        year=2021,
        description="Епічна науково-фантастична стрічка.",
        poster="https://cdn.example.test/posters/dune.jpg",
        translations=[Translation(id="uk", label="Дубляж")],
    )


def _serial() -> ContentResponse:
    return ContentResponse(
        id="p1:serial-1",
        type="series",
        title="Сериалал серіал",
        year=2023,
        description="Детективний серіал.",
        poster="https://cdn.example.test/posters/serial.jpg",
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


class _StreamStub(BaseProvider):
    """One home-capable provider whose ``stream()`` serves canned
    ``StreamResponse`` values and records the ``(id, translation)`` it
    was called with — so the D7 resolution seam is pinned."""

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(
        self,
        cards: list[SearchResult],
        content_by_external: dict[str, ContentResponse],
        streams: dict[str, StreamResponse],
    ) -> None:
        self._cards = cards
        self._content_by_external = content_by_external
        self._streams = streams
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
        content = self._content_by_external.get(external_id)
        if content is None:
            raise ProviderError("not_found", f"no canned content for {external_id}")
        return content.model_copy(deep=True)

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> StreamResponse:
        self.stream_calls.append((content_id, translation))
        stream = self._streams.get(content_id)
        if stream is None:
            raise ProviderError("not_found", f"no canned stream for {content_id}")
        return stream.model_copy(deep=True)


def _movie_stream(*, headers: bool) -> StreamResponse:
    return StreamResponse(
        url=f"{_CDN}/dune.mp4",
        type="mp4",
        headers=dict(_COMPANY_HEADERS) if headers else {},
    )


def _episode_stream(*, headers: bool) -> StreamResponse:
    return StreamResponse(
        url=f"{_CDN}/serial/s1e1/master.m3u8",
        type="m3u8",
        headers=dict(_COMPANY_HEADERS) if headers else {},
    )


def _seed(streams: dict[str, StreamResponse]) -> _StreamStub:
    return _StreamStub(
        cards=[
            SearchResult(
                id="p1:dune-1",
                provider="p1",
                type="movie",
                title="Дюна",
                year=2021,
                poster="https://cdn.example.test/posters/dune.jpg",
                url="https://p1.example/dune-1",
            ),
            SearchResult(
                id="p1:serial-1",
                provider="p1",
                type="series",
                title="Сериалал серіал",
                year=2023,
                poster="https://cdn.example.test/posters/serial.jpg",
                url="https://p1.example/serial-1",
            ),
        ],
        content_by_external={"dune-1": _dune(), "serial-1": _serial()},
        streams=streams,
    )


def _seeded(client: TestClient, *, headers: bool = False) -> tuple[_StreamStub, str, str]:
    """Register a stub with movie mp4 + episode m3u8 streams, warm the
    home snapshot, and return ``(stub, movie_gk, episode_wire_id)``."""
    stub = _seed(
        streams={
            "dune-1": _movie_stream(headers=headers),
            "s1e1": _episode_stream(headers=headers),
        }
    )
    return _home_seed(client, stub)


def _stale_memo() -> dict[Any, Any]:
    return cast("dict[Any, Any]", getattr(router_mod, "_STREAM_MEMO", {}))


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (_home_cache, _home_sources_cache, _content_cache, _blocklist_cache):
        cache.clear()
    _stale_memo().clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        for cache in (_home_cache, _home_sources_cache, _content_cache, _blocklist_cache):
            cache.clear()
        _stale_memo().clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def _home_seed(client: TestClient, stub: _StreamStub) -> tuple[_StreamStub, str, str]:
    """Register ``stub``, run /api/home once, pull the movie's ``g1:``
    group key and the episode wire id."""
    PROVIDERS["p1"] = stub
    r = client.get("/api/home")
    assert r.status_code == 200
    movie_gk = ""
    home = cast("dict[str, Any]", r.json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                movie_gk = cast(str, item["group_key"])
    assert movie_gk, "no movie group key in seeded home"
    return stub, movie_gk, "p1:s1e1"


# --- empty headers → 302 (no byte proxying) ----------------------------------


def test_stream_no_headers_redirects_to_cdn(client: TestClient) -> None:
    stub, gk, _ = _seeded(client, headers=False)

    r = client.get(
        f"/Videos/{gk}/stream", headers={"X-Emby-Token": TOKEN}, follow_redirects=False
    )

    assert r.status_code == 302
    assert r.headers["location"] == f"{_CDN}/dune.mp4"
    assert stub.stream_calls == [("dune-1", None)]  # one bare-id resolution


def test_episode_wire_id_with_slashes_reaches_stream(client: TestClient) -> None:
    """Episode wire ids can embed an upstream page URL containing ``/``
    (``p1:https://…/s/1/ep.html``). The client percent-encodes the id into
    the path; the path-converter route must still match and resolve."""
    stub, _, _ = _seeded(client, headers=False)
    ep_url = "https://cdn.example.test/s/s1/e1.m3u8"
    stub._streams[ep_url] = _movie_stream(headers=False)
    ep_id = f"p1:{ep_url}"

    r = client.get(
        f"/Videos/{quote(ep_id, safe='')}/stream",
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )

    assert r.status_code == 302
    assert r.headers["location"] == f"{_CDN}/dune.mp4"
    assert stub.stream_calls == [("https://cdn.example.test/s/s1/e1.m3u8", None)]


# --- mp4 file byte proxy -----------------------------------------------------


def test_stream_mp4_range_proxy(client: TestClient) -> None:
    """A provider-headered mp4 proxies bytes through the backend: the
    client's ``Range`` is forwarded, and 206 / Content-Range /
    Accept-Ranges / Content-Type come back from the CDN."""
    _, gk, _ = _seeded(client, headers=True)
    with respx.mock() as router:
        cdn = router.get(f"{_CDN}/dune.mp4").mock(
            return_value=httpx.Response(
                206,
                content=b"\x00" * 100,
                headers={
                    "Content-Range": "bytes 0-99/1000",
                    "Accept-Ranges": "bytes",
                    "Content-Type": "video/mp4",
                },
            )
        )
        with _fake_host():
            r = client.get(
                f"/Videos/{gk}/stream",
                headers={"X-Emby-Token": TOKEN, "Range": "bytes=0-99"},
            )

    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-99/1000"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == b"\x00" * 100
    req = cdn.calls.last.request
    assert req.headers["Referer"] == _COMPANY_HEADERS["Referer"]
    assert req.headers["Range"] == "bytes=0-99"
    assert len(cdn.calls) == 1


def test_stream_mp4_full_body_passthrough(client: TestClient) -> None:
    """Without a Range the proxy passes the whole body through with the
    upstream's Content-Type."""
    _, gk, _ = _seeded(client, headers=True)
    with respx.mock() as mlock:
        mlock.get(f"{_CDN}/dune.mp4").mock(
            return_value=httpx.Response(200, content=b"\x01\x02\x03", headers={"Content-Type": "video/mp4"})
        )
        with _fake_host():
            r = client.get(f"/Videos/{gk}/stream", headers={"X-Emby-Token": TOKEN})

    assert r.status_code == 200
    assert r.content == b"\x01\x02\x03"
    assert r.headers["content-type"] == "video/mp4"


# --- HLS manifest rewrite ----------------------------------------------------


def test_m3u8_manifest_rewrites_every_reference(client: TestClient) -> None:
    """The manifest body is rewritten so every media reference (segments
    AND ``URI=`` attributes: keys, maps, child playlists) becomes the
    backend segment proxy, relative refs resolved against the manifest
    URL. Served as mpegurl."""
    _, _, ep = _seeded(client, headers=True)
    manifest = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:6\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="caption.key"\n'
        "#EXTINF:6.0,\n"
        "seg000.ts\n"
        "#EXTINF:6.0,\n"
        "https://other.cdn.example.test/serial/seg001.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    with respx.mock() as mlock:
        cdn = mlock.get(f"{_CDN}/serial/s1e1/master.m3u8").mock(
            return_value=httpx.Response(
                200,
                content=manifest.encode(),
                headers={"Content-Type": "application/vnd.apple.mpegurl"},
            )
        )
        with _fake_host():
            r = client.get(f"/Videos/{ep}/stream", headers={"X-Emby-Token": TOKEN})

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.apple.mpegurl"
    # Relative segment → resolved against the manifest dir, rewritten.
    seg_url = f"/Videos/{ep}/segment?url={_ENCODED(f'{_CDN}/serial/s1e1/seg000.ts', safe='')}"
    assert seg_url in r.text
    # URI="..." attribute refs are rewritten too (key files ride the proxy).
    key_url = f"{_CDN}/serial/s1e1/caption.key"
    assert f'URI="{_ENCODED(key_url, safe="")}"' not in r.text  # not raw
    assert f'URI="/Videos/{ep}/segment?url={_ENCODED(key_url, safe="")}"' in r.text
    # Absolute references survive urljoin untouched.
    assert "other.cdn.example.test" in r.text
    # Every media reference in the rewritten manifest is either a comment
    # line or a backend segment-URL line — no raw upstream URI escaped.
    assert all(
        line.strip().startswith("#") or line.startswith("/Videos/") for line in r.text.splitlines()
    )
    req = cdn.calls.last.request
    assert req.headers["Referer"] == _COMPANY_HEADERS["Referer"]
    assert len(cdn.calls) == 1


def test_stream_m3u8_upstream_failure_404(client: TestClient) -> None:
    """A manifest fetch that fails upstream degrades to 404 (D2)."""
    _, _, ep = _seeded(client, headers=True)
    with respx.mock() as mlock:
        mlock.get(f"{_CDN}/serial/s1e1/master.m3u8").mock(return_value=httpx.Response(404))
        with _fake_host():
            r = client.get(f"/Videos/{ep}/stream", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 404


# --- segment proxy ------------------------------------------------------------


def test_segment_proxies_after_manifest(client: TestClient) -> None:
    """Client flow: open the manifest (memoizing provider headers), then
    a rewritten segment URL is fetched with the same headers."""
    _, _, ep = _seeded(client, headers=True)
    with respx.mock() as mlock:
        mlock.get(f"{_CDN}/serial/s1e1/master.m3u8").mock(
            return_value=httpx.Response(200, content="#EXTM3U\n#EXT-X-ENDLIST\n")
        )
        seg = mlock.get(f"{_CDN}/serial/s1e1/seg000.ts").mock(
            return_value=httpx.Response(200, content=b"\x00\x01", headers={"Content-Type": "video/mp2t"})
        )
        with _fake_host():
            client.get(f"/Videos/{ep}/stream", headers={"X-Emby-Token": TOKEN})
            r = client.get(
                f"/Videos/{ep}/segment",
                params={"url": f"{_CDN}/serial/s1e1/seg000.ts"},
                headers={"X-Emby-Token": TOKEN},
            )
    assert r.status_code == 200
    assert r.content == b"\x00\x01"
    assert r.headers["content-type"] == "video/mp2t"
    assert seg.calls.last.request.headers["Referer"] == _COMPANY_HEADERS["Referer"]


def test_segment_cold_resolves_stream_once(client: TestClient) -> None:
    """A segment request with no prior manifest fetch re-resolves the
    stream to learn provider headers + the CDN host, then proxies."""
    stub, _, ep = _seeded(client, headers=True)
    with respx.mock() as mlock:
        seg = mlock.get(f"{_CDN}/serial/s1e1/seg002.ts").mock(
            return_value=httpx.Response(200, content=b"\xaa")
        )
        with _fake_host():
            r = client.get(
                f"/Videos/{ep}/segment",
                params={"url": f"{_CDN}/serial/s1e1/seg002.ts"},
                headers={"X-Emby-Token": TOKEN},
            )
    assert r.status_code == 200
    assert r.content == b"\xaa"
    assert stub.stream_calls == [("s1e1", None)]
    assert seg.calls.last.request.headers["Referer"] == _COMPANY_HEADERS["Referer"]


def test_segment_variant_playlist_rewritten(client: TestClient) -> None:
    """A rewritten master may point at variant playlists, not segments:
    the segment proxy must treat a ``.m3u8`` reference as another
    manifest (fetch + re-rewrite) so a multi-level tree keeps every
    descendant reference pointed at the backend."""
    _, _, ep = _seeded(client, headers=True)
    child_url = f"{_CDN}/serial/s1e1/variant/index.m3u8"
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
        f"{child_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    child = (
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:6\n"
        "#EXTINF:6.0,\n"
        "seg003.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    with respx.mock() as mlock:
        mlock.get(f"{_CDN}/serial/s1e1/master.m3u8").mock(
            return_value=httpx.Response(200, content=master.encode())
        )
        child_route = mlock.get(child_url).mock(
            return_value=httpx.Response(
                200, content=child.encode(), headers={"Content-Type": "application/vnd.apple.mpegurl"}
            )
        )
        with _fake_host():
            r1 = client.get(f"/Videos/{ep}/stream", headers={"X-Emby-Token": TOKEN})
            assert r1.status_code == 200
            proxied_child = f"/Videos/{ep}/segment?url={_ENCODED(child_url, safe='')}"
            assert proxied_child in r1.text
            r2 = client.get(proxied_child, headers={"X-Emby-Token": TOKEN})

    assert r2.status_code == 200
    assert r2.headers["content-type"] == "application/vnd.apple.mpegurl"
    # The child's own segment reference was re-rewritten too.
    assert f"{_CDN}/serial/s1e1/variant/seg003.ts" not in r2.text
    assert f"/Videos/{ep}/segment?url={_ENCODED(f'{_CDN}/serial/s1e1/variant/seg003.ts', safe='')}" in r2.text
    assert child_route.calls.last.request.headers["Referer"] == _COMPANY_HEADERS["Referer"]


def test_segment_allows_sibling_cdn_subdomain(client: TestClient) -> None:
    """A multi-level HLS tree may hand child playlists to a SIBLING
    subdomain of the same registrable domain (api.example.test next to
    cdn.example.test). The CDN check anchors on the registrable domain,
    not the first hostname seen, so the variant fetch succeeds."""
    _, _, ep = _seeded(client, headers=True)
    child_url = "https://api.example.test/serial/s1e1/variant/sd.m3u8"
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
        f"{child_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    child = (
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:6\n"
        "#EXTINF:6.0,\n"
        "seg003.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    with respx.mock() as mlock:
        mlock.get(f"{_CDN}/serial/s1e1/master.m3u8").mock(
            return_value=httpx.Response(200, content=master.encode())
        )
        mlock.get(child_url).mock(
            return_value=httpx.Response(
                200, content=child.encode(), headers={"Content-Type": "application/vnd.apple.mpegurl"}
            )
        )
        with _fake_host():
            r1 = client.get(f"/Videos/{ep}/stream", headers={"X-Emby-Token": TOKEN})
            assert r1.status_code == 200
            proxied_child = f"/Videos/{ep}/segment?url={_ENCODED(child_url, safe='')}"
            assert proxied_child in r1.text
            r2 = client.get(proxied_child, headers={"X-Emby-Token": TOKEN})

    assert r2.status_code == 200
    assert r2.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert (
        f"/Videos/{ep}/segment?url={_ENCODED('https://api.example.test/serial/s1e1/variant/seg003.ts', safe='')}"
        in r2.text
    )


def test_segment_rejects_foreign_host(client: TestClient) -> None:
    """SSRF posture: a segment URL outside the item's CDN (dot-boundary)
    fails closed to 404 before any upstream request is issued."""
    _, _, ep = _seeded(client, headers=True)
    evil = "https://evil.example/steal.ts"
    with respx.mock(assert_all_called=False) as mlock:
        evil_route = mlock.get(evil).mock(return_value=httpx.Response(200, content=b"nope"))
        with _fake_host():
            r = client.get(
                f"/Videos/{ep}/segment",
                params={"url": evil},
                follow_redirects=False,
                headers={"X-Emby-Token": TOKEN},
            )
    assert r.status_code == 404
    assert not evil_route.called


def test_stream_and_segment_are_public(client: TestClient) -> None:
    """Stream and segment routes are public (no token required): the PS4
    media player fetches the stream URL directly and does not carry auth
    headers. This mirrors the public-image-route convention."""
    assert client.get("/Videos/g1:abcdefabcdefabcdef/stream").status_code == 404
    assert (
        client.get("/Videos/g1:abcdefabcdefabcdef/segment", params={"url": f"{_CDN}/s.ts"}).status_code
        == 404
    )