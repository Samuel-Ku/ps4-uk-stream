"""Jellyfin REST wire shapes for the facade (spec D1/D2/D4, ticket #102).

Only the fields real Jellyfin clients (Switchfin, web/desktop) actually
consume are modelled. The shapes are deliberately *minimal*: the facade
tells clients a coherent story (a Jellyfin server behind a fixed opaque
token) without carrying the full server's JSON surface.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class BaseItemDto(BaseModel):
    """Minimal Jellyfin ``BaseItemDto`` (spec D5/D9, ticket #104).

    Only the fields a Switchfin-style client consumes for library and
    item-card rendering: identity, display name, media type, year,
    parent view id, and the primary-image tag. ``ImageTags`` is the
    dict clients read to decide whether art exists for the item; the
    ``Primary`` value is an opaque cache-buster derived from the poster
    URL (D9: tag present *iff* the item has a poster).
    """

    Name: str | None = None
    ServerId: str | None = None
    Id: str | None = None
    Type: str | None = None
    CollectionType: str | None = None
    ProductionYear: int | None = None
    ParentId: str | None = None
    ImageTags: dict[str, str] = Field(default_factory=dict)


class BaseItemDtoQueryResult(BaseModel):
    """``ItemsResult``-style envelope every list endpoint returns.

    ``TotalRecordCount`` is what Jellyfin clients use to render
    scrolling; the facade caps every listing at the home row size (20),
    so it always equals ``len(Items)`` (D5: no pagination in v1).
    """

    Items: list[BaseItemDto] = Field(default_factory=list)
    TotalRecordCount: int = 0