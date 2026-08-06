"""Jellyfin facade router (spec #100, tickets #102 + #104).

Mounted on the existing FastAPI app at the Jellyfin paths — deliberately
NOT under ``/api/*``, so the native contract is untouched and a Jellyfin
client pointed at ``host:port`` finds a server without configuration.

Ticket #102 scope: the handshake. Ticket #104 scope: the catalog
surface — views, item listing, poster. Later tickets add item detail
(#105), search (#106), PlaybackInfo (#107), the conditional stream
handler (#108-#110) and sessions behind the same ``require_token`` gate.
"""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..catalog_state import get_home, load_home
from ..config import SETTINGS
from ..models import HomeItem, HomeRow
from .auth import require_token
from .models import (
    AuthenticationResult,
    BaseItemDto,
    BaseItemDtoQueryResult,
    SystemInfoPublic,
    UserDto,
)

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


#: The home-row routing keys that can exist in a snapshot (v3 spec §3.1).
#: A view's ``Id`` is a deterministic uuid5 of one of these keys, so the
#: mapping is reversible (``_view_type_by_id``) and stable across
#: restarts — a client's cached library list keeps working.
_VIEW_TYPES = ("newest", "popular", "movie", "series", "anime", "cartoon", "dorama")

_VIEW_ID_BY_TYPE = {
    t: uuid.uuid5(uuid.NAMESPACE_URL, f"cs-uk-api-view:{t}").hex for t in _VIEW_TYPES
}
_VIEW_TYPE_BY_ID = {vid: t for t, vid in _VIEW_ID_BY_TYPE.items()}

#: Jellyfin ``CollectionType`` per home-row routing key (D5). The movie
#: row maps to the standard ``movies``; every other row is episodic-ish
#: and maps to ``tvshows``. No client we target branches deeper than
#: movie-vs-tvshows here.
_COLLECTION_TYPE_BY_ROW = {
    "movie": "movies",
    "series": "tvshows",
    "anime": "tvshows",
    "cartoon": "tvshows",
    "dorama": "tvshows",
    "newest": "tvshows",
    "popular": "tvshows",
}

#: Home ``MediaType`` → Jellyfin item Type. Only Movie/Series are expressible
#: on the wire (AC: "correct Type (Movie/Series)"); style-tagged rows are
#: episodic content and become Series.
_JF_TYPE_BY_ROW = {
    "movie": "Movie",
    "series": "Series",
    "anime": "Series",
    "cartoon": "Series",
    "dorama": "Series",
}


def _poster_tag(poster_url: str) -> str:
    """Opaque ``ImageTags.Primary`` value (D9).

    Deterministic in the poster URL, so a client-side image cache
    busts exactly when the upstream art changes and not otherwise.
    """
    return hashlib.sha256(poster_url.encode()).hexdigest()[:16]


def _row_dto(row: HomeRow, server_id: str) -> BaseItemDto:
    """One virtual library (D5): a ``CollectionFolder`` whose ``Id`` the
    client echoes back as ``parentId`` on ``/Items``."""
    return BaseItemDto(
        Name=row.title,
        ServerId=server_id,
        Id=_VIEW_ID_BY_TYPE[row.type],
        Type="CollectionFolder",
        CollectionType=_COLLECTION_TYPE_BY_ROW.get(row.type),
    )


def _item_dto(row: HomeRow, item: HomeItem, server_id: str) -> BaseItemDto:
    """One library card: Movie/Series item carrying the ``g1:`` id.

    ``ImageTags.Primary`` is set only when the card carries a poster
    (D9). ``year`` is surfaced as ``ProductionYear`` (Jellyfin's field);
    ``ParentId`` is the view the card came from.
    """
    dto = BaseItemDto(
        Name=item.title,
        ServerId=server_id,
        Id=item.group_key,
        Type=_JF_TYPE_BY_ROW.get(item.type, "Series"),
        ProductionYear=item.year,
        ParentId=_VIEW_ID_BY_TYPE[row.type],
    )
    if item.poster is not None:
        dto.ImageTags = {"Primary": _poster_tag(item.poster)}
    return dto


def _poster_for(item_id: str) -> str | None:
    """The canonical poster URL for a ``g1:`` item id, or None.

    Resolution walks the cached home snapshot — the same lookup the
    native ``/api/content/{group_key}`` route uses — and takes the
    card's first-seen poster. A cold cache yields None → 404 ("item
    unavailable"), which Jellyfin clients tolerate per D2. Deliberately
    does NOT trigger a home build: an image request must not fan out to
    every provider.
    """
    home = get_home()
    if home is None:
        return None
    for row in home.rows:
        for it in row.items:
            if it.group_key == item_id:
                return it.poster
    return None


async def _user_views() -> BaseItemDtoQueryResult:
    """One virtual library per ``/api/home`` row, in home-row order (D5).

    Triggers the shared home build on a cold cache (the same cost as
    ``GET /api/home``), so a fresh client launch never sees an empty
    library list; afterwards it serves from the 30-min snapshot.
    """
    home = await load_home()
    server_id = _server_id()
    dtos = [_row_dto(row, server_id) for row in home.rows]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


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


@router.get(
    "/UserViews",
    response_model=BaseItemDtoQueryResult,
    dependencies=[Depends(require_token)],
)
async def user_views_sdk(
    user_id: str | None = Query(default=None, alias="userId"),
) -> BaseItemDtoQueryResult:
    """SDK spelling of the views call (capture report, ticket #103).

    The official ``@jellyfin/sdk`` sends bare ``/UserViews?userId=…``
    rather than the server-style ``/Users/{id}/Views``; both spellings
    are served (capture verdict, ticket #103). The echoed ``User.Id``
    carries no server-side meaning — every client on this LAN is the
    same viewer.
    """
    return await _user_views()


@router.get(
    "/Users/{user_id}/Views",
    response_model=BaseItemDtoQueryResult,
    dependencies=[Depends(require_token)],
)
async def user_views_server(user_id: str) -> BaseItemDtoQueryResult:
    """Server-style spelling of the views call (spec D5)."""
    return await _user_views()


@router.get(
    "/Items",
    response_model=BaseItemDtoQueryResult,
    dependencies=[Depends(require_token)],
)
async def items_listing(
    parent_id: str | None = Query(default=None, alias="parentId"),
    user_id: str | None = Query(default=None, alias="userId"),
) -> BaseItemDtoQueryResult:
    """Library listing for one view (capture report: bare ``/Items``).

    ``parentId`` is the view's ``Id`` echoed from ``/UserViews``.
    Unknown or absent view → empty result, matching Jellyfin's tolerant
    answer for a stale parent (D5: no pagination in v1; each row is
    capped at 20 by the home builder).
    """
    row_type = _VIEW_TYPE_BY_ID.get(parent_id or "")
    if row_type is None:
        return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)
    home = await load_home()
    server_id = _server_id()
    for row in home.rows:
        if row.type == row_type:
            dtos = [_item_dto(row, it, server_id) for it in row.items]
            return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))
    # A valid view id whose row is currently absent (e.g. «Популярні
    # зараз» when no provider carries it) is an empty library, not an
    # error — same tolerant answer as an unknown parent.
    return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)


@router.get(
    "/Items/{item_id}/Images/Primary",
    dependencies=[Depends(require_token)],
)
async def item_primary_image(item_id: str) -> RedirectResponse:
    """Poster art (D9): 302 to the existing poster proxy.

    ``maxWidth``/``tag`` and friends are ignored — the original image
    is served, no resizing in v1. Unknown item or poster-less item →
    404; Jellyfin clients render a placeholder instead of an image.
    """
    poster_url = _poster_for(item_id)
    if poster_url is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    return RedirectResponse(
        url=f"/api/poster?u={quote(poster_url, safe='')}",
        status_code=302,
    )


__all__ = ["require_token", "router"]