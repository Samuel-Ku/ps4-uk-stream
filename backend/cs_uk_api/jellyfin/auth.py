"""Facade auth: the fixed-token handshake (spec D4, ticket #102).

The Jellyfin facade is deliberately unauthenticated in the login sense —
any username/password completes the handshake — but every subsequent
request must present the fixed opaque ``jellyfin_token`` (env
``CS_UK_JF_TOKEN``) in one of the two header forms real clients use:

  - ``X-Emby-Token: <token>``
  - ``Authorization: MediaBrowser Token="<token>"``

Translated into FastAPI terms: one ``Depends`` that raises 401 when the
token is absent or wrong. Public endpoints (``/System/Info/Public`` and
``POST /Users/AuthenticateByName``) simply do not declare the dependency.
"""

from __future__ import annotations

import logging
import re

from fastapi import Header, HTTPException, Query, status

from .. import config as _config

log = logging.getLogger("cs_uk_api.jellyfin")

#: ``Authorization: MediaBrowser Token="<token>"`` — the form Jellyfin
#: clients send on authenticated requests. Real clients (web, desktop,
#: Switchfin, and the capture driver's SDK) prefix the header with their
#: client/device identity — ``MediaBrowser Client="…", Device="…",
#: DeviceId="…", Version="…", Token="<token>"`` — so the token field is
#: matched anywhere after the ``MediaBrowser`` scheme, not anchored at
#: the start. ``Token`` is unquoted in the wild too, so accept both.
_MEDIA_BROWSER_TOKEN_RE = re.compile(
    r'^\s*MediaBrowser\s+.*\bToken\s*=\s*("([^"]*)"|([^,\s]+))\s*$',
    re.IGNORECASE | re.DOTALL,
)

#: 401 semantics: Jellyfin answers sync with 401 + a JSON body carrying
#: a sequence-delimited error token. The exact body differs across
#: client generations, so any 401 with an HTML-ish identity challenge
#: header is enough; Switchfin reloads the login flow on any 401.
_UNAUTHORIZED = status.HTTP_401_UNAUTHORIZED


def _media_browser_token(authorization: str | None) -> str | None:
    """Extract the token from ``Authorization: MediaBrowser Token=...``.

    Returns ``None`` when the header is absent or not a MediaBrowser
    token form (e.g. a ``Bearer`` credential).
    """
    if not authorization:
        return None
    m = _MEDIA_BROWSER_TOKEN_RE.match(authorization)
    if not m:
        return None
    # Group 2 = the quoted form, group 3 = the unquoted token. Exactly
    # one is set; re.match guarantees at least one branch matched.
    token = m.group(2) or m.group(3)
    return token or None


def resolve_token(
    x_emby_token: str | None = None,
    authorization: str | None = None,
    api_key: str | None = None,
) -> str | None:
    """The client-presented token from any supported channel, else None."""
    if x_emby_token:
        return x_emby_token.strip()
    if api_key:
        return api_key.strip()
    return _media_browser_token(authorization)


async def require_token(
    x_emby_token: str | None = Header(default=None, alias="X-Emby-Token"),
    authorization: str | None = Header(default=None),
    api_key: str | None = Query(default=None, alias="api_key"),
) -> str:
    """FastAPI dependency enforcing the fixed facade token (D4).

    Accepts the token via ``X-Emby-Token`` header, ``Authorization:
    MediaBrowser Token=...`` header, or ``?api_key=...`` query parameter
    (the form media-player requests like ``/Videos/{id}/stream`` use,
    since the player URL is built from PlaybackInfo's ``Path`` field and
    the client appends ``api_key`` rather than a header). Logs a warning
    on rejection so a misconfigured client is debuggable without exposing
    the token value.
    """
    presented = resolve_token(
        x_emby_token=x_emby_token,
        authorization=authorization,
        api_key=api_key,
    )
    if presented is None:
        log.warning("jellyfin facade rejecting request: missing token header")
        raise HTTPException(status_code=_UNAUTHORIZED, detail="missing token")
    if presented != _config.SETTINGS.jellyfin_token:
        log.warning("jellyfin facade rejecting request: wrong token")
        raise HTTPException(status_code=_UNAUTHORIZED, detail="invalid token")
    return presented