from __future__ import annotations

from urllib.parse import urljoin, urlparse

import httpx

from . import config as _config
from .providers.base import BaseProvider, ProviderError

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(_config.SETTINGS.upstream_timeout_s),
            headers={"User-Agent": "cs-uk-api/1.0 (+https://github.com/)"},
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def safe_get(
    http: httpx.AsyncClient,
    url: str,
    *,
    allowed_hosts: set[str],
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """GET with manual redirect handling and SSRF protection.

    `follow_redirects` is disabled on the shared client so redirects
    don't silently cross to arbitrary hosts. The INITIAL request URL
    must also be in `allowed_hosts` — it often comes from (possibly
    decrypted) upstream HTML, so a hostile CMS could otherwise point
    the backend at an arbitrary host and have it fetched directly from
    its LAN position. When the upstream returns a 3xx with a `Location`
    header, the redirect target's netloc must be in `allowed_hosts`;
    otherwise the call raises `ProviderError("not_found", ...)`.
    Allowed redirects are followed by re-invoking the helper so the
    host check applies to every hop.
    """
    initial_host = urlparse(url).netloc
    if initial_host not in allowed_hosts:
        raise ProviderError(
            "not_found", f"disallowed host: {initial_host}"
        )
    response = await http.get(
        url, follow_redirects=False, headers=headers, params=params
    )
    if 300 <= response.status_code < 400:
        location = response.headers.get("Location")
        if location:
            # A Location may be relative (e.g. `/sezon-1/ep.html` on
            # DLE sites); resolve it against the request URL BEFORE the
            # host check so the netloc comparison sees an absolute URL.
            resolved = urljoin(url, location)
            host = urlparse(resolved).netloc
            if host not in allowed_hosts:
                raise ProviderError(
                    "not_found", f"redirect to disallowed host: {host}"
                )
            return await safe_get(
                http,
                resolved,
                allowed_hosts=allowed_hosts,
                headers=headers,
                params=None,
            )
    return response


async def provider_safe_get(
    http: httpx.AsyncClient,
    provider: BaseProvider,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """``safe_get`` whose allowlist IS the provider's declaration (ADR-0005).

    The single fetch path for adapters: every hop — the initial URL and
    each redirect target — is checked against ``provider.allowed_hosts``,
    so a provider that never declared its hosts cannot fetch anything
    (the empty default fails closed), and an upstream-derived URL cannot
    escape the declared hosts. Adapters no longer pass host sets by
    hand; the declaration lives on the adapter class.
    """
    return await safe_get(
        http,
        url,
        allowed_hosts=set(provider.allowed_hosts),
        headers=headers,
        params=params,
    )
