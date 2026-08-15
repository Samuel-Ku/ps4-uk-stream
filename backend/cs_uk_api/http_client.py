from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .config import SETTINGS
from .providers.base import ProviderError, ProviderErrorCode

_client: httpx.AsyncClient | None = None

#: Sentinel for ``safe_get``'s ``allowed_hosts`` parameter: distinguishes
#: "the caller did not pass it" (→ the fail-closed default) from an
#: explicit ``None`` (the documented escape hatch that skips the check).
#: ``Any`` (not ``object``) so the ``= _UNSET`` argument default type-
#: checks under mypy strict; only identity comparisons are ever made.
_UNSET: Any = object()

#: Default upstream SSRF allowlist (spec #309 T6, US7): a fetch whose
#: caller did not declare hosts is checked against THIS set. Empty by
#: default = fail-closed — the guard is the default, and a fetch without
#: an allowlist raises ``not_found`` instead of leaking an SSRF surface.
#: The poster allowlist is deliberately NOT the upstream default (poster
#: hosts are a different trust domain).
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset()


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(SETTINGS.upstream_timeout_s),
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
    allowed_hosts: set[str] | None = _UNSET,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """GET with manual redirect handling and SSRF protection.

    The host allowlist applies BY DEFAULT (spec #309 T6, US7):

      - omitted → ``DEFAULT_ALLOWED_HOSTS`` (empty = fail-closed: the
        fetch raises ``not_found`` — the guard is the default, never
        the exception);
      - a set → the check runs against exactly those hosts;
      - ``None`` → the documented escape hatch: the host check is
        skipped for this call (use only where the URL is not
        attacker-influenced).

    `follow_redirects` is disabled on the shared client so redirects
    don't silently cross to arbitrary hosts. The INITIAL request URL
    must also be in `allowed_hosts` — it often comes from (possibly
    decrypted) upstream HTML, so a hostile CMS could otherwise point
    the backend at an arbitrary host and have it fetched directly from
    its LAN position. When the upstream returns a 3xx with a `Location`
    header, the redirect target's netloc must be in `allowed_hosts`;
    otherwise the call raises `ProviderError(ProviderErrorCode.NOT_FOUND,
    ...)`. Allowed redirects are followed by re-invoking the helper so
    the host check applies to every hop.
    """
    if allowed_hosts is _UNSET:
        allowed_hosts = set(DEFAULT_ALLOWED_HOSTS)
    if allowed_hosts is not None:
        initial_host = urlparse(url).netloc
        if initial_host not in allowed_hosts:
            raise ProviderError(
                ProviderErrorCode.NOT_FOUND, f"disallowed host: {initial_host}"
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
            if allowed_hosts is not None:
                host = urlparse(resolved).netloc
                if host not in allowed_hosts:
                    raise ProviderError(
                        ProviderErrorCode.NOT_FOUND, f"redirect to disallowed host: {host}"
                    )
            return await safe_get(
                http,
                resolved,
                allowed_hosts=allowed_hosts,
                headers=headers,
                params=None,
            )
    return response