"""The facade's user-state conversation (spec #257).

ONE owner for the routes where the viewer changes THEIR state:
favorites and played marks, each returning the updated ``UserDataResult``
so the client's heart/check button updates from the response (a bare
204 leaves the button stuck). State is single-user (D4) and persists in
the versioned user-state file owned by the catalog seam.

Import direction: this module -> catalog (set_favorite/set_played),
resolution (_user_data). It receives the ``router`` at registration
time.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..catalog import set_favorite, set_played
from .auth import require_token
from .models import UserDataResult
from .resolution import _user_data


def register(router: APIRouter) -> None:
    """Attach the user-state conversation to the facade router.

    Declaration order is preserved (favorite add/remove before played
    add/remove), exactly as in the original router.py.
    """

    @router.post(
        "/Users/{user_id}/FavoriteItems/{item_id}",
        response_model=UserDataResult,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def favorite_add(user_id: str, item_id: str) -> UserDataResult:
        """Favorite an item (spec #257).

        The RESPONSE is the UserDataResult — Switchfin updates its heart
        button from the response's ``IsFavorite``, so a bare 204 would
        leave the button stuck. State is single-user (D4) and persists in
        the versioned user-state file.
        """
        set_favorite(item_id, True)
        return _user_data(item_id)  # type: ignore[return-value]

    @router.delete(
        "/Users/{user_id}/FavoriteItems/{item_id}",
        response_model=UserDataResult,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def favorite_remove(user_id: str, item_id: str) -> UserDataResult:
        """Un-favorite an item (spec #257) — same response contract."""
        set_favorite(item_id, False)
        return _user_data(item_id)  # type: ignore[return-value]

    @router.post(
        "/Users/{user_id}/PlayedItems/{item_id}",
        response_model=UserDataResult,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def played_add(user_id: str, item_id: str) -> UserDataResult:
        """Mark an item played (spec #257) — the context-menu affordance."""
        set_played(item_id, True)
        return _user_data(item_id)  # type: ignore[return-value]

    @router.delete(
        "/Users/{user_id}/PlayedItems/{item_id}",
        response_model=UserDataResult,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    async def played_remove(user_id: str, item_id: str) -> UserDataResult:
        """Mark an item unplayed (spec #257) — same response contract."""
        set_played(item_id, False)
        return _user_data(item_id)  # type: ignore[return-value]
