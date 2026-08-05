"""Jellyfin REST wire shapes for the facade (spec D1/D2/D4, ticket #102).

Only the fields real Jellyfin clients (Switchfin, web/desktop) actually
consume are modelled. The shapes are deliberately *minimal*: the facade
tells clients a coherent story (a Jellyfin server behind a fixed opaque
token) without carrying the full server's JSON surface.
"""

from __future__ import annotations

from pydantic import BaseModel


class SystemInfoPublic(BaseModel):
    """Response of ``GET /System/Info/Public``.

    ``Id`` is a stable free-form string the client echoes back to the
    ``X-Emby-Client-Concurrency``-less server in later handshakes; the
    facade accepts any value readers send, so a stable constant suffices.
    """

    LocalAddress: str
    SystemName: str
    Version: str
    ProductName: str
    StartupWizardCompleted: bool
    Id: str


class UserDto(BaseModel):
    """The ``User`` object returned by AuthenticateByName.

    Only ``Id`` and ``Name`` are consumed downstream (`/Users/{id}/Views`);
    everything else is filler that keeps non-Switchfin clients happy.
    """

    Name: str
    ServerId: str
    Id: str
    Configuration: str | None = None
    Policy: str | None = None
    PrimaryImageTag: str | None = None


class AuthenticationResult(BaseModel):
    """``POST /Users/AuthenticateByName`` response: the token handshake.

    ``AccessToken`` is the fixed opaque ``jellyfin_token`` setting. The
    session is stateless — no session store exists (D8) — so
    ``SessionInfo`` carries only the server id.
    """

    User: UserDto
    AccessToken: str
    ServerId: str
    SessionInfo: str | None = None