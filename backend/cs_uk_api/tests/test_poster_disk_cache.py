"""Poster disk-cache tests (issue #54, v3 spec §8).

Seam under test: ``poster_proxy.fetch`` — a second fetch of the same URL
within the 7-day TTL must be served from disk without an upstream request;
expired entries re-fetch; disk failures degrade gracefully to a fresh
upstream fetch (never break serving).
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import replace

import httpx
import pytest
import respx

from cs_uk_api import poster_proxy
from cs_uk_api.config import SETTINGS
from cs_uk_api.poster_proxy import fetch

_URL = "https://anitube.in.ua/uploads/poster.jpg"
_BODY = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # minimal jpeg-ish bytes
_PUBLIC_IP = "93.184.216.34"


def _getaddrinfo_public(host: str, port: int | None) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]


@pytest.fixture(autouse=True)
def _dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        poster_proxy,
        "SETTINGS",
        replace(
            SETTINGS,
            poster_allowed_hosts=("anitube.in.ua",),
            poster_cache_dir=str(tmp_path / "posters"),
        ),
    )
    poster_proxy._cache.clear()
    yield tmp_path / "posters"
    poster_proxy._cache.clear()


def _mock_upstream() -> respx.MockRouter:
    router = respx.MockRouter()
    router.get(_URL).mock(return_value=httpx.Response(200, content=_BODY, headers={"Content-Type": "image/jpeg"}))
    return router


async def test_first_fetch_writes_disk_second_is_served_from_disk(_settings):
    with _mock_upstream() as router:
        async with httpx.AsyncClient() as http:
            first = await fetch(_URL, http)
            assert first == (_BODY, "image/jpeg")
            assert len(router.calls) == 1

            # Memory cache cleared: only the disk layer can serve now.
            poster_proxy._cache.clear()
            second = await fetch(_URL, http)
            assert second == (_BODY, "image/jpeg")
            assert len(router.calls) == 1  # no second upstream request


async def test_expired_disk_entry_refetches(_settings):
    with _mock_upstream() as router:
        async with httpx.AsyncClient() as http:
            await fetch(_URL, http)
            assert len(router.calls) == 1

            # Age all cache files beyond the 7-day TTL.
            old = time.time() - 8 * 24 * 3600
            for f in _settings.iterdir():
                os.utime(f, (old, old))

            poster_proxy._cache.clear()
            await fetch(_URL, http)
            assert len(router.calls) == 2


async def test_content_type_round_trips_through_extension(_settings):
    url_png = "https://anitube.in.ua/uploads/poster.png"
    with respx.MockRouter() as router:
        router.get(url_png).mock(
            return_value=httpx.Response(200, content=_BODY, headers={"Content-Type": "image/png"})
        )
        async with httpx.AsyncClient() as http:
            await fetch(url_png, http)
            poster_proxy._cache.clear()
            assert await fetch(url_png, http) == (_BODY, "image/png")


async def test_non_image_content_type_is_normalized_to_jpeg(_settings):
    url = "https://anitube.in.ua/uploads/weird"
    with respx.MockRouter() as router:
        router.get(url).mock(
            return_value=httpx.Response(200, content=_BODY, headers={"Content-Type": "text/plain"})
        )
        async with httpx.AsyncClient() as http:
            assert await fetch(url, http) == (_BODY, "image/jpeg")
            poster_proxy._cache.clear()
            assert await fetch(url, http) == (_BODY, "image/jpeg")


async def test_unwritable_cache_dir_falls_back_to_upstream(_settings, monkeypatch):
    with _mock_upstream() as router:
        async with httpx.AsyncClient() as http:
            monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("ro fs")))
            assert await fetch(_URL, http) == (_BODY, "image/jpeg")  # serving unaffected
            monkeypatch.undo()
            assert len(router.calls) == 1


async def test_size_cap_still_enforced_on_disk_writes(_settings, monkeypatch):
    big = b"\xff" * (SETTINGS.poster_size_cap_bytes + 1)
    with respx.MockRouter() as router:
        router.get(_URL).mock(return_value=httpx.Response(200, content=big, headers={"Content-Type": "image/jpeg"}))
        async with httpx.AsyncClient() as http:
            assert await fetch(_URL, http) is None
            poster_proxy._cache.clear()
            assert await fetch(_URL, http) is None
            assert not _settings.exists() or not list(_settings.iterdir())  # nothing on disk either
