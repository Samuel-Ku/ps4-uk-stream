"""Tests for the shared HTTP client helper (``safe_get``)."""

from __future__ import annotations

import httpx
import pytest
import respx

from cs_uk_api.http_client import provider_safe_get, safe_get
from cs_uk_api.providers.base import ProviderError


@pytest.mark.asyncio
async def test_safe_get_follows_relative_redirect():
    """A 301 with a RELATIVE Location must be resolved against the
    request URL before the host check — otherwise the netloc is empty
    and the whole fetch aborts with `redirect to disallowed host`."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.test/a/").respond(
            301, headers={"Location": "/b.html"}
        )
        router.get("https://example.test/b.html").respond(200, text="ok")
        async with httpx.AsyncClient() as http:
            r = await safe_get(
                http,
                "https://example.test/a/",
                allowed_hosts={"example.test"},
            )
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.asyncio
async def test_safe_get_follows_absolute_redirect():
    """Absolute Locations keep working after the resolution change."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.test/a/").respond(
            301, headers={"Location": "https://cdn.example.test/b"}
        )
        router.get("https://cdn.example.test/b").respond(200, text="ok")
        async with httpx.AsyncClient() as http:
            r = await safe_get(
                http,
                "https://example.test/a/",
                allowed_hosts={"example.test", "cdn.example.test"},
            )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_safe_get_rejects_cross_host_redirect():
    """A redirect to a host outside ``allowed_hosts`` fails closed."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.test/a/").respond(
            301, headers={"Location": "https://evil.test/steal"}
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError):
                await safe_get(
                    http, "https://example.test/a/", allowed_hosts={"example.test"}
                )


@pytest.mark.asyncio
async def test_safe_get_rejects_disallowed_initial_host():
    """The INITIAL URL must also be checked against ``allowed_hosts``.

    ``safe_get`` is used for player-hop requests whose URL comes from
    (possibly decrypted) upstream HTML. A hostile CMS could return a
    player URL pointing at an arbitrary host; the fetch must fail closed
    before any network I/O, or the backend becomes an open fetcher from
    its LAN position (SSRF).
    """
    with respx.mock(assert_all_called=False) as router:
        # Route is registered only so a buggy (fetching) impl makes a
        # clean network call instead of an unmocked-NetworkError; the
        # assertion that matters is the ProviderError below.
        router.get("https://evil.test/steal").respond(200, text="stolen")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await safe_get(
                    http, "https://evil.test/steal", allowed_hosts={"example.test"}
                )
    assert "disallowed host" in exc_info.value.message


# ---------------------------------------------------------------------------
# provider_safe_get — the ADR-0005 declaration-enforcing wrapper
# ---------------------------------------------------------------------------


class _Allowlisted:
    """Stand-in for a provider instance: just the declaration."""

    id = "stub"
    allowed_hosts = frozenset({"example.test"})


@pytest.mark.asyncio
async def test_provider_safe_get_uses_the_provider_declaration():
    """The wrapper reads ``provider.allowed_hosts`` so adapters never
    pass host sets by hand."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.test/a").respond(200, text="ok")
        async with httpx.AsyncClient() as http:
            r = await provider_safe_get(http, _Allowlisted(), "https://example.test/a")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_provider_safe_get_fails_closed_without_a_declaration():
    """An adapter that OMITS ``allowed_hosts`` (the empty default) can
    fetch nothing — omission fails closed instead of failing open."""
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.test/a").respond(200, text="ok")
        async with httpx.AsyncClient() as http:

            class _Undeclared(_Allowlisted):
                allowed_hosts = frozenset()

            with pytest.raises(ProviderError) as exc_info:
                await provider_safe_get(http, _Undeclared(), "https://example.test/a")
    assert "disallowed host" in exc_info.value.message


@pytest.mark.asyncio
async def test_provider_safe_get_rejects_redirect_off_the_declaration():
    """Redirect hops are checked against the SAME declaration."""
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.test/a/").respond(
            301, headers={"Location": "https://evil.test/steal"}
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError):
                await provider_safe_get(
                    http, _Allowlisted(), "https://example.test/a/"
                )