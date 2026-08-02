"""Poster proxy SSRF hardening tests (map ticket "Poster proxy policy — SSRF hardening").

Policy (decided 2026-08-02): host allowlist (provider domains + known
poster CDNs, suffix matching on dot boundaries) AND private/loopback/
link-local range blocking via DNS resolution of every hop, including
redirect targets. Redirects are followed manually, hop-capped, with
each hop re-validated.
"""

from __future__ import annotations

import socket
from dataclasses import replace

import httpx
import pytest
import respx

from cs_uk_api import poster_proxy
from cs_uk_api.config import SETTINGS
from cs_uk_api.poster_proxy import fetch, is_allowed

_TEST_ALLOWLIST = ("anitube.in.ua", "uakino.club", "unimay.media")

_PUBLIC_IP = "93.184.216.34"


def _getaddrinfo_public(host: str, port: int | None) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setattr(
        poster_proxy,
        "SETTINGS",
        # poster_cache_dir=None: keep these tests on the memory layer; the
        # disk layer is exercised in test_poster_disk_cache.py.
        replace(SETTINGS, poster_allowed_hosts=_TEST_ALLOWLIST, poster_cache_dir=None),
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    poster_proxy._cache.clear()


# ---------- default allowlist covers every provider domain ----------


def test_default_allowlist_covers_provider_domains():
    from cs_uk_api.config import DEFAULT_POSTER_ALLOWED_HOSTS

    provider_domains = (
        "animeon.club",
        "animeua.club",
        "anitube.in.ua",
        "bambooua.com",
        "cikava-ideya.top",
        "coani.net",
        "doramy.world",
        "eneyida.tv",
        "hentaiukr.com",
        "kinotron.tv",
        "kinovezha.tv",
        "klon.fun",
        "serialno.tv",
        "simpsonsua.tv",
        "uafix.net",
        "uakino.best",
        "uakino.club",
        "uaserials.com",
        "ufdub.com",
        "unimay.media",
    )
    assert set(provider_domains) <= set(DEFAULT_POSTER_ALLOWED_HOSTS)


# ---------- is_allowed: scheme + host allowlist ----------


def test_is_allowed_rejects_non_http_schemes():
    for u in ("ftp://anitube.in.ua/x.jpg", "file:///etc/passwd", "javascript:alert(1)"):
        assert is_allowed(u) is False


def test_is_allowed_rejects_host_outside_allowlist():
    assert is_allowed("https://evil.example.com/x.jpg") is False


def test_is_allowed_accepts_provider_domain():
    assert is_allowed("https://anitube.in.ua/uploads/x.jpg") is True


def test_is_allowed_accepts_cdn_subdomain_of_allowlisted_domain():
    assert is_allowed("https://cdn.uakino.club/thumbs/1.jpg") is True


def test_is_allowed_rejects_suffix_spoofing():
    assert is_allowed("https://anitube.in.ua.evil.com/x.jpg") is False


def test_is_allowed_rejects_private_literal_ips():
    for u in (
        "http://127.0.0.1:8080/admin",
        "http://192.168.2.223/x.jpg",
        "https://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/x.jpg",
    ):
        assert is_allowed(u) is False


# ---------- DNS layer: allowlisted host must resolve publicly ----------


@pytest.mark.asyncio
async def test_fetch_refuses_host_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    with respx.mock(assert_all_called=False):
        result = await fetch("https://anitube.in.ua/x.jpg", httpx.AsyncClient())
    assert result is None


@pytest.mark.asyncio
async def test_fetch_refuses_when_dns_fails(monkeypatch):
    def _no_dns(host, port):
        raise socket.gaierror("no address")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    with respx.mock(assert_all_called=False):
        result = await fetch("https://anitube.in.ua/x.jpg", httpx.AsyncClient())
    assert result is None


# ---------- fetch: happy path and redirect handling ----------


@pytest.mark.asyncio
async def test_fetch_returns_image_for_allowed_host():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://anitube.in.ua/x.jpg").mock(
            return_value=httpx.Response(200, content=b"\x89PNG", headers={"Content-Type": "image/png"})
        )
        result = await fetch("https://anitube.in.ua/x.jpg", httpx.AsyncClient())
    assert result == (b"\x89PNG", "image/png")


@pytest.mark.asyncio
async def test_fetch_does_not_follow_redirect_to_disallowed_host():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://anitube.in.ua/x.jpg").mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example.com/y.jpg"})
        )
        result = await fetch("https://anitube.in.ua/x.jpg", httpx.AsyncClient())
    assert result is None


@pytest.mark.asyncio
async def test_fetch_follows_redirect_to_allowlisted_host():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://anitube.in.ua/x.jpg").mock(
            return_value=httpx.Response(302, headers={"Location": "https://cdn.uakino.club/y.jpg"})
        )
        router.get("https://cdn.uakino.club/y.jpg").mock(
            return_value=httpx.Response(200, content=b"IMG", headers={"Content-Type": "image/webp"})
        )
        result = await fetch("https://anitube.in.ua/x.jpg", httpx.AsyncClient())
    assert result == (b"IMG", "image/webp")


@pytest.mark.asyncio
async def test_fetch_bounds_redirect_chain():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://anitube.in.ua/a.jpg").mock(
            return_value=httpx.Response(302, headers={"Location": "https://cdn.uakino.club/b.jpg"})
        )
        router.get("https://cdn.uakino.club/b.jpg").mock(
            return_value=httpx.Response(302, headers={"Location": "https://anitube.in.ua/a.jpg"})
        )
        result = await fetch("https://anitube.in.ua/a.jpg", httpx.AsyncClient())
    assert result is None


# ---------- fetch: response guards (existing behaviour) ----------


@pytest.mark.asyncio
async def test_fetch_rejects_oversized_response():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://anitube.in.ua/x.jpg").mock(
            return_value=httpx.Response(200, content=b"x" * (SETTINGS.poster_size_cap_bytes + 1))
        )
        result = await fetch("https://anitube.in.ua/x.jpg", httpx.AsyncClient())
    assert result is None


@pytest.mark.asyncio
async def test_fetch_defaults_non_image_content_type_to_jpeg():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://anitube.in.ua/x.jpg").mock(
            return_value=httpx.Response(200, content=b"<html>hi</html>", headers={"Content-Type": "text/html"})
        )
        result = await fetch("https://anitube.in.ua/x.jpg", httpx.AsyncClient())
    assert result == (b"<html>hi</html>", "image/jpeg")
