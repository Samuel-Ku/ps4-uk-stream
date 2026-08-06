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
    """Minimal Jellyfin ``BaseItemDto`` (spec D5/D9, ticket #104; detail
    + hierarchy fields, ticket #105).

    Only the fields a Switchfin-style client consumes for library and
    item-card rendering: identity, display name, media type, year,
    parent view id, and the primary-image tag. ``ImageTags`` is the
    dict clients read to decide whether art exists for the item; the
    ``Primary`` value is an opaque cache-buster derived from the poster
    URL (D9: tag present *iff* the item has a poster).

    Ticket #105 adds the detail + hierarchy surface a client needs to
    render one item's page and to walk Series → Season → Episode:
    ``Overview`` (description), ``IndexNumber``/``ParentIndexNumber``
    (position inside a season / season number) and ``SeriesId`` (the
    owning series' ``g1:`` key) on Season/Episode items.
    """

    Name: str | None = None
    ServerId: str | None = None
    Id: str | None = None
    Type: str | None = None
    CollectionType: str | None = None
    ProductionYear: int | None = None
    ParentId: str | None = None
    ImageTags: dict[str, str] = Field(default_factory=dict)
    Overview: str | None = None
    IndexNumber: int | None = None
    ParentIndexNumber: int | None = None
    SeriesId: str | None = None
    SeriesName: str | None = None


class BaseItemDtoQueryResult(BaseModel):
    """``ItemsResult``-style envelope every list endpoint returns.

    ``TotalRecordCount`` is what Jellyfin clients use to render
    scrolling; the facade caps every listing at the home row size (20),
    so it always equals ``len(Items)`` (D5: no pagination in v1).
    """

    Items: list[BaseItemDto] = Field(default_factory=list)
    TotalRecordCount: int = 0


class MediaStreamInfo(BaseModel):
    """One entry of ``MediaSourceInfo.MediaStreams`` (spec D6, ticket #106).

    Deliberately ONLY ``{"Type": "Video"}`` — no codec, resolution, or
    bitrate fields. Lying about codecs risks forcing a transcode path
    the facade cannot serve; ``IsDirectStream: true`` tells the client
    the bytes are what they are.
    """

    Type: str = "Video"


class MediaSourceInfo(BaseModel):
    """The thin ``MediaSources[0]`` of PlaybackInfo (spec D6).

    ``Id`` = the item id (the ``g1:`` group key or the provider-scoped
    episode wire id); ``Container`` = the provider's ``StreamResponse``
    type (mp4/m3u8/hls); ``Path`` is a FICTITIOUS stable string — the
    bytes always come from ``/Videos/{id}/stream``, never from Path.
    """

    Id: str
    Container: str
    MediaStreams: list[MediaStreamInfo] = Field(default_factory=lambda: [MediaStreamInfo()])
    IsDirectStream: bool = True
    Path: str
    PlaySessionId: str


class PlaybackInfoResponse(BaseModel):
    """``GET/POST /Items/{id}/PlaybackInfo`` envelope (spec D6).

    Exactly one MediaSource. The top-level ``PlaySessionId`` mirrors the
    source's — real Jellyfin echoes it in both places, and clients read
    either.
    """

    MediaSources: list[MediaSourceInfo]
    PlaySessionId: str