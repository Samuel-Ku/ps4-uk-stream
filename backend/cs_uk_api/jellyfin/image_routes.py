"""The facade's image conversation (ticket #104, D9).

ONE owner for the art routes a client's image loaders hit: the poster
(``Primary``), the avatar placeholder, and the Backdrop/Logo/Thumb
aliases. All are public on purpose (token-less image loading — the
client's loaders never attach ``X-Emby-Token``) and answer inline —
never a redirect (Switchfin's loader doesn't chase one; a 302 rendered
as an error storm, observed ~72 attempts per poster).

Import direction: this module -> images (the WebP verdict/transcode),
poster_proxy, catalog (content-cache poster fallback), resolution
(poster lookup). It receives the ``router`` at registration time.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..catalog import resolve_item
from ..http_client import get_client
from ..poster_proxy import fetch as fetch_poster_bytes
from ..wire_identity import is_group_key
from . import images
from .resolution import _poster_for


def register(router: APIRouter) -> None:
    """Attach the image conversation to the facade router.

    Routes stay PUBLIC (no ``require_token``) and keep their relative
    declaration order; ``Primary`` before the Backdrop/Logo/Thumb
    aliases, exactly as in the original router.py.
    """

    @router.get(
        "/Items/{item_id}/Images/Primary",
    )
    async def item_primary_image(
        item_id: str, format: str | None = None, maxWidth: int | None = None
    ) -> Response:
        """Poster art (D9): the poster bytes, served directly with 200.

        The client's own ``format=Webp`` / ``maxWidth`` query (Switchfin
        always asks for ``Webp``) is honored: a non-WebP original is
        transcoded once (Pillow, resized to ``maxWidth`` when the original
        is larger) and cached per poster. Unknown item or poster-less item →
        404; Jellyfin clients render a placeholder instead of an image.

        Public on purpose: Jellyfin serves images without a token (media
        is addressable by URL), and client image loaders do not attach the
        ``X-Emby-Token`` header — requiring one here produces a wall of
        ``401 Unauthorized`` console errors.

        The bytes are fetched via the same ``fetch_poster_bytes`` cache the
        native ``/api/poster`` route uses, and returned inline — NOT as a
        302 to it. Switchfin's image loader does not chase a redirect; a
        redirect status is rendered as an error storm ("302") on the
        console while the home screen retries each card's art dozens of
        times (observed: ~72 attempts per poster).
        """
        return await _serve_item_image(item_id, format=format, maxWidth=maxWidth)

    @router.get(
        "/Users/{user_id}/Images/Primary",
    )
    async def user_primary_image(user_id: str, format: str | None = None) -> Response:
        """User avatar — no user concept on the facade.

        A transparent placeholder is served instead of a 404: Switchfin's
        server list always requests the avatar and logs "http status 404"
        on the console when it's missing. Public like every other image
        endpoint (token-less image loading, see ``item_primary_image``).
        """
        body, ctype = images.placeholder_avatar(format)
        return Response(content=body, media_type=ctype)

    @router.get(
        "/Items/{item_id}/Images/Thumb",
    )
    @router.get(
        "/Items/{item_id}/Images/Logo",
    )
    @router.get(
        "/Items/{item_id}/Images/Backdrop",
    )
    @router.get(
        "/Items/{item_id}/Images/Backdrop/{index}",
    )
    async def item_auxiliary_image(item_id: str, index: int = 0) -> Response:
        """Backdrop/Logo/Thumb art — same poster bytes as ``Primary``.

        The catalog stores a single poster per item (no fanart, logos, or
        backdrops), so each variant serves that same image inline with 200,
        matching Switchfin's probe expectations (``apiThumbImage``/
        ``apiLogoImage``/``apiBackdropImage``). Public like all image
        endpoints; unknown/poster-less item → 404 (the client treats that
        as "no such art").
        """
        return await _serve_item_image(item_id)


async def _serve_item_image(
    item_id: str, *, format: str | None = None, maxWidth: int | None = None
) -> Response:
    """The poster for ``item_id`` as an inline image response, or 404.

    Resolution (poster URL lookup + the shared poster-cache fetch) stays
    here; the WebP verdict/transcode delegates to the image module
    (ticket #343). ``fetch_poster_bytes`` resolves through THIS module at
    call time — the suite stubs it here.
    """
    poster_url = _poster_for(item_id)
    if poster_url is None and is_group_key(item_id):
        # Item not in the home snapshot (surfaced via Latest/search);
        # resolve from the content cache which holds the poster URL.
        content = (await resolve_item(item_id)).content
        poster_url = content.poster if content else None
    if poster_url is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    fetched = await fetch_poster_bytes(poster_url, get_client())
    if fetched is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    body, ctype = fetched
    if images.wants_webp(format) and not ctype.startswith("image/webp"):
        body = images.as_webp(poster_url, body, maxWidth)
        ctype = "image/webp"
    return Response(content=body, media_type=ctype)


__all__ = ["register"]
