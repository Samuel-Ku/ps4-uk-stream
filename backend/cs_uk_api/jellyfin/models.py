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
    ServerName: str
    Version: str
    ProductName: str
    StartupWizardCompleted: bool
    Id: str
    SystemArchitecture: str = ""


class UserDto(BaseModel):
    """The ``User`` object returned by AuthenticateByName.

    Only ``Id`` and ``Name`` are consumed downstream (`/Users/{id}/Views`);
    everything else is filler that keeps non-Switchfin clients happy.

    The filler must NOT be JSON-null: Switchfin parses this struct with
    ``NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE_WITH_DEFAULT`` and any ``null``
    value where it expects a string/object raises nlohmann's
    ``type_error.302`` ("type must be string, but is null"), which that
    console shows as a bare "302". So ``PrimaryImageTag`` defaults to an
    empty string and the two nested config blocks to empty objects.
    """

    Name: str
    ServerId: str
    Id: str
    HasPassword: bool = False
    Configuration: dict[str, object] = Field(default_factory=dict)
    Policy: dict[str, bool] = Field(
        default_factory=lambda: {"IsAdministrator": False, "IsDisabled": False}
    )
    PrimaryImageTag: str = ""


class AuthenticationResult(BaseModel):
    """``POST /Users/AuthenticateByName`` response: the token handshake.

    ``AccessToken`` is the fixed opaque ``jellyfin_token`` setting. The
    session is stateless — no session store exists (D8) — so
    ``SessionInfo`` carries only the server id.
    """

    User: UserDto
    AccessToken: str
    ServerId: str
    SessionInfo: object | None = None


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
    owning series' ``g2:`` key) on Season/Episode items.
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
    PlaybackPositionTicks: int | None = None
    #: Genre shelf (ticket #213): the genre name as both Id and Name
    #: (Jellyfin's own convention — genre ids ARE the names), and
    #: ChildCount = how many cards of the view carry the genre.
    ChildCount: int | None = None
    #: Free-form genre labels on an item (ticket #213) — the detail
    #: surface renders them (``media_movie``/``media_series`` show a
    #: ``labelGenres`` row when non-empty).
    Genres: list[str] = Field(default_factory=list)
    #: Cast rail (ticket #221) — present iff the resolved provider's
    #: content page exposed cast; Switchfin hides the People header
    #: when the list is empty, so the rail simply doesn't appear for
    #: providers without cast data.
    People: list[PersonDto] = Field(default_factory=list)


class PersonDto(BaseModel):
    """One entry of ``BaseItemDto.People`` (ticket #221).

    Jellyfin's wire shape is ``{Id, Name, PrimaryImageTag, Role}``;
    Switchfin's ``media_series``/``media_movie`` populate the People
    rail from this list and only render it when non-empty. ``Id`` is
    the provider-scoped person key that round-trips through
    ``/Persons/{id}``.
    """

    Id: str
    Name: str
    Role: str = "Actor"


class BaseItemDtoQueryResult(BaseModel):
    """``ItemsResult``-style envelope every list endpoint returns.

    ``TotalRecordCount`` is what Jellyfin clients use to render
    scrolling; ``Items`` is the requested ``startIndex``/``limit`` page of
    the (home-row-capped, 20-item) listing, and ``TotalRecordCount`` stays
    the full count so a client knows more pages exist (device-driving B11:
    ignoring the slice made page 2 repeat page 1 and the real client's
    infinite scroll looped on it forever).

    ``StartIndex`` must be present: Switchfin's ``Result<T>`` wrapper is
    parsed via ``NLOHMANN_JSON_FROM`` (no default), so a missing key
    raises ``out_of_range.403`` on the console. Echoes the requested page.
    """

    Items: list[BaseItemDto] = Field(default_factory=list)
    TotalRecordCount: int = 0
    StartIndex: int = 0


class DisplayPreferencesDto(BaseModel):
    """Response of ``GET /DisplayPreferences/usersettings``.

    Switchfin parses via ``NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE_WITH_DEFAULT``
    (Id, CustomPrefs, SortBy, SortOrder) — every field tolerates absence,
    but an explicit null would trip ``type_error.302``, so the defaults
    are strings/dicts rather than None. ``Id`` echoes the request's
    settings key (``usersettings``).
    """

    Id: str = "usersettings"
    CustomPrefs: dict[str, object] = Field(default_factory=dict)
    SortBy: str = "SortName"
    SortOrder: str = "Ascending"


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

    ``Id`` = the item id (the ``g2:`` group key or the provider-scoped
    episode wire id); ``Container`` = the provider's ``StreamResponse``
    type (mp4/m3u8/hls); ``Path`` is a FICTITIOUS stable string — the
    bytes always come from ``/Videos/{id}/stream``, never from Path.
    """

    Id: str
    Container: str
    MediaStreams: list[MediaStreamInfo] = Field(default_factory=lambda: [MediaStreamInfo()])
    IsDirectStream: bool = True
    SupportsDirectPlay: bool = True
    SupportsDirectStream: bool = True
    SupportsTranscoding: bool = False
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


class SearchHint(BaseModel):
    """One entry of ``SearchHintResult.SearchHints`` (spec D10, ticket #106).

    The search-box surface some clients hit (``GET /Search/Hints``) —
    the same merged-group card as the ``/Items?searchTerm=`` listing, in
    hint shape: ``ItemId`` is the ``g2:`` group key the detail/image
    routes resolve, ``Type`` the Movie/Series verdict. ``ImageTags``
    present *iff* the card has a poster (D9), mirroring ``BaseItemDto``.
    """

    ItemId: str | None = None
    Id: str | None = None
    Name: str | None = None
    Type: str | None = None
    ProductionYear: int | None = None
    ImageTags: dict[str, str] = Field(default_factory=dict)


class SearchHintResult(BaseModel):
    """``GET /Search/Hints`` envelope (spec D10, ticket #106)."""

    SearchHints: list[SearchHint] = Field(default_factory=list)
    TotalRecordCount: int = 0