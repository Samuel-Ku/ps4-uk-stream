"""Jellyfin facade router (spec #100, ticket #102 skeleton).

Mounted on the existing FastAPI app at the Jellyfin paths — deliberately
NOT under ``/api/*``, so the native contract is untouched and a Jellyfin
client pointed at ``host:port`` finds a server without configuration.

Ticket #102 scope: the handshake. Later tickets add Views (/Users/{id}/
Views), Items, PlaybackInfo, the conditional stream handler, sessions
and posters behind the same ``require_token`` gate.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..config import SETTINGS
from .auth import require_token
from .models import AuthenticationResult, SystemInfoPublic, UserDto

router = APIRouter(tags=["jellyfin"])

#: What the server tells the client it is. The real Jellyfin server name
#: is configurable; we surface the project's own identity so the
#: client's connection dialog shows something recognizable.
_PRODUCT = "cs-uk-api"
_VERSION = "0.1.0"


def _server_id() -> str:
    """Stable per-process identity: deterministic hash of host:port.

    A restart keeps the same ServerId (clients pin it in their local
    database), while two different deployments differ.
    """
    return hashlib.sha256(f"{SETTINGS.host}:{SETTINGS.port}".encode()).hexdigest()[:16]


class AuthenticateByNameRequest(BaseModel):
    """Login body. Any username/password completes the handshake (D4)."""

    Username: str = ""
    Pw: str = ""


@router.get("/System/Info/Public", response_model=SystemInfoPublic)
async def system_info_public() -> SystemInfoPublic:
    """Server discovery: what a client hits first when adding the server.

    Unauthenticated by design (D4): the client needs this to render the
    login screen at all.
    """
    return SystemInfoPublic(
        LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
        SystemName=_PRODUCT,
        Version=_VERSION,
        ProductName=_PRODUCT,
        StartupWizardCompleted=True,
        Id=_server_id(),
    )


@router.get("/System/Info", response_model=SystemInfoPublic, dependencies=[Depends(require_token)])
async def system_info(
    _token: str = Depends(require_token),
) -> SystemInfoPublic:
    """Full server info — authenticated in real Jellyfin.

    A client that has completed the handshake fetches this to confirm
    the server identity; the web UI reads ``SystemName``/``Version`` off
    it when reconnecting to a cached server. The first private facade
    route: proves the ``require_token`` gate on a real endpoint.
    """
    return SystemInfoPublic(
        LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
        SystemName=_PRODUCT,
        Version=_VERSION,
        ProductName=_PRODUCT,
        StartupWizardCompleted=True,
        Id=_server_id(),
    )


@router.post("/Users/AuthenticateByName", response_model=AuthenticationResult)
async def authenticate_by_name(
    body: AuthenticateByNameRequest,
) -> AuthenticationResult:
    """Accept-any-credentials login (D4): return the fixed token.

    The request username is echoed back as the user's name so the
    client's "signed in as X" UI shows what the user typed; nothing is
    stored (sessions are no-ops, D8).
    """
    token = SETTINGS.jellyfin_token
    server_id = _server_id()
    user = UserDto(
        Name=body.Username,
        ServerId=server_id,
        Id=uuid.uuid5(uuid.NAMESPACE_URL, f"cs-uk-api-user:{body.Username}").hex,
        Configuration=None,
        Policy=None,
        PrimaryImageTag=None,
    )
    return AuthenticationResult(
        User=user,
        AccessToken=token,
        ServerId=server_id,
        SessionInfo=None,
    )


__all__ = ["require_token", "router"]