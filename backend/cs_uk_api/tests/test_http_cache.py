"""Tests for the conditional-GET cache (``cs_uk_api.http_cache``)."""

from __future__ import annotations

import gzip
from pathlib import Path

import httpx
import pytest

from cs_uk_api.http_cache import CacheSummary, CachingTransport, ConditionalCache

URL = "https://example.test/browse/films/p1"


def _client(handler, cache: ConditionalCache) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=CachingTransport(httpx.MockTransport(handler), cache)
    )


@pytest.mark.asyncio
async def test_etag_revalidation_replays_the_cached_body(tmp_path: Path) -> None:
    seen: list[str | None] = []
    body = "<html><body>Dune</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(
            200,
            headers={"ETag": '"v1"', "Content-Type": "text/html; charset=utf-8"},
            text=body,
        )

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        first = await client.get(URL)
        second = await client.get(URL)
    cache.close()

    assert seen == [None, '"v1"']
    assert first.status_code == second.status_code == 200
    assert second.text == body
    assert cache.summary() == CacheSummary(
        stored=1, revalidated=1, bytes_saved=len(body.encode())
    )


@pytest.mark.asyncio
async def test_last_modified_is_used_when_there_is_no_etag(tmp_path: Path) -> None:
    seen: list[str | None] = []
    last_modified = "Wed, 01 Oct 2026 00:00:00 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-modified-since"))
        if request.headers.get("if-modified-since") == last_modified:
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={
                "Last-Modified": last_modified,
                "Content-Type": "text/html",
            },
            text="page",
        )

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        await client.get(URL)
        second = await client.get(URL)
    cache.close()

    assert seen == [None, last_modified]
    assert second.text == "page"


@pytest.mark.asyncio
async def test_a_response_without_validators_is_never_cached(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, text="no validators here")

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        await client.get(URL)
        await client.get(URL)
    cache.close()

    assert seen == [None, None]
    assert cache.summary().stored == 0


@pytest.mark.asyncio
async def test_error_responses_are_not_cached(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(404, headers={"ETag": '"gone"'}, text="nope")

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        await client.get(URL)
        await client.get(URL)
    cache.close()

    assert seen == [None, None]
    assert cache.summary().stored == 0


@pytest.mark.asyncio
async def test_binary_bodies_are_not_stored(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(
            200,
            headers={"ETag": '"png"', "Content-Type": "image/png"},
            content=b"\x89PNG\r\n",
        )

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        await client.get(URL)
        await client.get(URL)
    cache.close()

    assert seen == [None, None]
    assert cache.summary().stored == 0


@pytest.mark.asyncio
async def test_validators_survive_between_clients(tmp_path: Path) -> None:
    """The nightly run is a fresh process: the cache must be on disk."""
    path = tmp_path / "cache.sqlite3"
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": '"v1"'}, text="kept")

    first_cache = ConditionalCache(path)
    async with _client(handler, first_cache) as client:
        await client.get(URL)
    first_cache.close()

    second_cache = ConditionalCache(path)
    async with _client(handler, second_cache) as client:
        replay = await client.get(URL)
    second_cache.close()

    assert seen == [None, '"v1"']
    assert replay.text == "kept"


@pytest.mark.asyncio
async def test_a_callers_own_condition_is_passed_through(tmp_path: Path) -> None:
    """A caller that asked its own question gets the origin's real verdict."""
    cache = ConditionalCache(tmp_path / "cache.sqlite3")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"caller"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": '"v1"'}, text="cached")

    async with _client(handler, cache) as client:
        await client.get(URL)  # prime
        answer = await client.get(URL, headers={"If-None-Match": '"caller"'})
    cache.close()

    assert answer.status_code == 304


@pytest.mark.asyncio
async def test_an_invalid_last_modified_is_never_replayed(tmp_path: Path) -> None:
    """ufdub sends ``... -1100 GMT``; echoing that could earn a 400."""
    seen: list[str | None] = []
    bad = "Wed, 21 Oct 2026 20:12:15 -1100 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-modified-since"))
        return httpx.Response(
            200, headers={"Last-Modified": bad}, text="listing"
        )

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        await client.get(URL)
        await client.get(URL)
    cache.close()

    assert seen == [None, None]
    assert cache.summary().stored == 0


@pytest.mark.asyncio
async def test_no_store_listings_are_still_revalidated(tmp_path: Path) -> None:
    """Documented trade-off: many DLE hosts say no-store yet honour 304.

    The body is only replayed after the origin itself confirmed it, so the
    stock ``no-store`` header does not veto revalidation here.
    """
    last_modified = "Fri, 30 Oct 2026 17:10:26 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-modified-since") == last_modified:
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Last-Modified": last_modified,
                "Content-Type": "text/html",
            },
            text="listing",
        )

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        await client.get(URL)
        replay = await client.get(URL)
    cache.close()

    assert replay.text == "listing"
    assert cache.summary().revalidated == 1


def test_the_body_budget_evicts_the_oldest_entry(tmp_path: Path) -> None:
    cache = ConditionalCache(tmp_path / "cache.sqlite3", max_bytes=10)
    cache.put("a", body=b"123456", headers=[], etag='"a"', last_modified=None)
    cache.put("b", body=b"123456", headers=[], etag='"b"', last_modified=None)

    assert cache.get("a") is None
    assert cache.get("b") is not None
    cache.close()


@pytest.mark.asyncio
async def test_gzip_pages_are_stored_decoded(tmp_path: Path) -> None:
    """The replay must be plain text even though the origin sent gzip."""
    body = "<html>стиснуто</html>".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"gz"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={
                "ETag": '"gz"',
                "Content-Encoding": "gzip",
                "Content-Type": "text/html; charset=utf-8",
            },
            content=gzip.compress(body),
        )

    cache = ConditionalCache(tmp_path / "cache.sqlite3")
    async with _client(handler, cache) as client:
        first = await client.get(URL)
        second = await client.get(URL)
    cache.close()

    assert first.text == second.text == body.decode()
