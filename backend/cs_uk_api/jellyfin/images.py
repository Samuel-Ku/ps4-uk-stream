"""Image pipeline for the Jellyfin facade routes (ticket #343).

The image-shaped logic behind ``/Items/{id}/Images/*`` and
``/Users/{id}/Images/Primary`` lives here: the memoized WebP transcode,
the transparent placeholder avatar, and the ``format=Webp`` verdict the
client's query maps to. The router keeps the route declarations and the
bytes resolution (poster URL lookup + the shared poster cache); this
module turns bytes into the response body.

Pillow stays imported LAZILY inside the transcode helpers (the fresh-env
install ships it, but importing it at module import would pay the cost
on every backend start for a surface most requests never touch).
"""

from __future__ import annotations

import io

#: WebP spellings Switchfin sends (its PS4 build decodes WebP whenever
#: the URL contains ``Webp``; ``webp,kwebp`` is its compound form).
_WEBP_FORMATS = ("webp", "webp,kwebp")


def wants_webp(image_format: str | None) -> bool:
    """Whether an image request's ``format`` asks for WebP bytes.

    The parameter is named for keyword use — ``format`` shadows a
    builtin, so callers pass ``image_format=format``.
    """
    return image_format is not None and image_format.lower().lstrip("/") in _WEBP_FORMATS


#: Transcode memo: ``(poster_url, max_width) → WebP bytes``. The client
#: retries a bad poster dozens of times per second (observed 533), so
#: the conversion must cost one Pillow pass per poster, not per request.
_WEBP_MEMO: dict[tuple[str, int | None], bytes] = {}
_WEBP_MEMO_MAX = 256


def as_webp(poster_url: str, body: bytes, max_width: int | None) -> bytes:
    """``body`` as WebP bytes (Pillow), resized to ``max_width`` when
    larger; the original back on any decode error (a transcode failure
    must not turn a served poster into a 404)."""
    key = (poster_url, max_width)
    hit = _WEBP_MEMO.get(key)
    if hit is not None:
        return hit
    try:
        from PIL import Image, ImageOps

        logo: Image.Image = Image.open(io.BytesIO(body))
        if max_width and logo.width > max_width:
            logo = ImageOps.contain(logo, (max_width, max_width))
        out = io.BytesIO()
        logo.convert("RGB").save(out, format="WEBP", quality=82)
        hit = out.getvalue()
    except Exception:  # noqa: BLE001
        hit = body
    if len(_WEBP_MEMO) >= _WEBP_MEMO_MAX:
        _WEBP_MEMO.clear()
    _WEBP_MEMO[key] = hit
    return hit


#: Placeholder avatar bytes per format — the facade has no user concept,
#: but Switchfin's server list ALWAYS requests each saved user's avatar
#: (``apiUserImage``) and its HTTP layer logs any 4xx as a console error
#: ("http status 404"). A transparent placeholder answers 200 while still
#: rendering as "no avatar": the client's own default glyph shows through
#: the transparency.
_AVATAR_MEMO: dict[str, bytes] = {}


def placeholder_avatar(image_format: str | None) -> tuple[bytes, str]:
    """A transparent placeholder image in the requested format.

    Switchfin's PS4 build requests ``format=Webp`` and decodes the body
    as WebP whenever the URL contains ``Webp`` (``Image::doRequest``), so
    the placeholder must be real WebP bytes — a PNG answer to a WebP URL
    silently fails to decode and renders nothing.
    """
    webp = wants_webp(image_format)
    key = "webp" if webp else "png"
    hit = _AVATAR_MEMO.get(key)
    if hit is not None:
        return hit, "image/webp" if webp else "image/png"
    from PIL import Image

    img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    out = io.BytesIO()
    img.save(out, format="WEBP" if webp else "PNG")
    hit = out.getvalue()
    _AVATAR_MEMO[key] = hit
    return hit, "image/webp" if webp else "image/png"


__all__ = ["as_webp", "placeholder_avatar", "wants_webp"]
