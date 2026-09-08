"""The facade's handshake conversation (ticket #102, spec D4).

ONE owner for the routes a client touches to BECOME connected: server
discovery, authenticated identity, the accept-any-credentials login,
and the views listing that turns the catalog into virtual libraries.
Browsing lives in ``router.py``; the dashboard shims live there too —
this module is only "get the client onto the server".

Import direction: this module -> identity (its own package's identity
conversation), dto/models, catalog (the views build). It receives the
``router`` at registration time and never imports ``router``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from .. import row_kinds
from ..catalog import refresh_snapshot
from ..config import SETTINGS
from ..models import HomeRow
from . import dto
from .auth import require_token
from .identity import _PRODUCT, _VERSION, _server_id, _user_name_for
from .models import (
    AuthenticationResult,
    BaseItemDto,
    BaseItemDtoQueryResult,
    SystemInfoPublic,
    UserDto,
)
from .resolution import _view_id_for


class AuthenticateByNameRequest(BaseModel):
    """Login body. Any username/password completes the handshake (D4)."""

    Username: str = ""
    Pw: str = ""


def _row_dto(row: HomeRow, server_id: str) -> BaseItemDto:
    """One virtual library (D5): a ``CollectionFolder`` whose ``Id`` the
    client echoes back as ``parentId`` on ``/Items``.

    The CollectionType derives from the row-kind table (spec #362 B):
    a table kind carries its entry's mapping; rows outside the table
    (the recipe-inserted personalized rows, the ``genre:<slug>`` rails)
    stay CollectionType-less.
    """
    entry = row_kinds.ROW_KINDS.get(row.type)
    return dto.row_dto(
        row.title,
        server_id,
        view_id=_view_id_for(row.type),
        collection_type=entry.collection_type if entry is not None else None,
    )


async def _user_views() -> BaseItemDtoQueryResult:
    """One virtual library per ``/api/home`` row, in home-row order (D5).

    Triggers the shared home build on a cold cache (the same cost as
    ``GET /api/home``), so a fresh client launch never sees an empty
    library list; afterwards it serves from the 30-min snapshot.
    """
    home = await refresh_snapshot()
    server_id = _server_id()
    dtos = [_row_dto(row, server_id) for row in home.rows]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


def register(router: APIRouter) -> None:
    """Attach the handshake conversation to the facade router.

    Declaration order is preserved relative to the original router.py
    (system info before QuickConnect, the literal ``/Users/{user_id}``
    before the parameterized spellings) so the route table is identical.
    """

    @router.get(
        "/System/Info/Public", response_model=SystemInfoPublic, response_model_exclude_none=True
    )
    async def system_info_public() -> SystemInfoPublic:
        """Server discovery: what a client hits first when adding the server.

        Unauthenticated by design (D4): the client needs this to render the
        login screen at all.
        """
        return SystemInfoPublic(
            LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
            ServerName=_PRODUCT,
            Version=_VERSION,
            ProductName=_PRODUCT,
            StartupWizardCompleted=True,
            Id=_server_id(),
        )

    @router.get(
        "/System/Info",
        response_model=SystemInfoPublic,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def system_info(
        _token: str = Depends(require_token),
    ) -> SystemInfoPublic:
        """Full server info — authenticated in real Jellyfin.

        A client that has completed the handshake fetches this to confirm
        the server identity; the web UI reads ``ServerName``/``Version`` off
        it when reconnecting to a cached server. The first private facade
        route: proves the ``require_token`` gate on a real endpoint.
        """
        return SystemInfoPublic(
            LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
            ServerName=_PRODUCT,
            Version=_VERSION,
            ProductName=_PRODUCT,
            StartupWizardCompleted=True,
            Id=_server_id(),
        )

    @router.get("/QuickConnect/Enabled", response_model=bool)
    async def quickconnect_enabled() -> bool:
        """Advertise that QuickConnect login is off.

        Switchfin probes this before rendering the login screen and compares
        the raw body to ``"true"`` before showing the Quick Connect button.
        Real Jellyfin answers with a bare boolean, so the facade mirrors
        that: ``false`` keeps the client on the password path.
        """
        return False

    @router.get(
        "/Branding/Configuration", response_model=dict[str, object], response_model_exclude_none=True
    )
    async def branding_configuration() -> dict[str, object]:
        """Empty branding block — the client falls back to defaults.

        Probed alongside ``/QuickConnect/Enabled`` during login-screen
        render. ``LoginDisclaimer`` must be a string, NOT null — Switchfin
        parses it into ``std::string`` via
        ``NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE_WITH_DEFAULT`` and a null value
        raises ``type_error.302`` on the console.
        """
        return {"LoginDisclaimer": ""}

    @router.get(
        "/Plugins",
        response_model=list[object],
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def plugins() -> list[object]:
        """Plugin listing — always empty.

        Switchfin probes this on EVERY app start (``AppConfig::checkDanmuku``)
        to detect the Danmu plugin; an unimplemented route answered 404 and
        the client's HTTP layer logged "http status 404" on the console. An
        empty ``PluginList`` (bare JSON array) means "no plugins" and
        disables danmaku cleanly.
        """
        return []

    @router.get(
        "/Users/{user_id}",
        response_model=UserDto,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def user_info(user_id: str) -> UserDto:
        """Persist a client's remembered session (Switchfin ``checkLogin``).

        On every start Switchfin calls ``GET /Users/{id}`` with the stored
        token to decide whether the previous login is still valid (config.cpp
        ``checkLogin``): a 200+parseable User keeps it in the main screen,
        anything else bounces it back to the login form. Since the facade is
        stateless and accepts any valid token, the remembered user is
        confirmed with a 200 echoing a stable UserDto — the client then skips
        re-authentication entirely.
        """
        return UserDto(
            Name=_user_name_for(user_id),
            ServerId=_server_id(),
            Id=user_id,
        )

    @router.post(
        "/Users/AuthenticateByName",
        response_model=AuthenticationResult,
        response_model_exclude_none=True,
    )
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
        response_model_exclude_none=True,
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
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def user_views_server(user_id: str) -> BaseItemDtoQueryResult:
        """Server-style spelling of the views call (spec D5)."""
        return await _user_views()
