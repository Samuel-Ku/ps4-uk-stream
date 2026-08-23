"""Unit tests for the extracted image pipeline module (ticket #343).

The route-level behaviour (inline poster serving, avatar placeholders,
the WebP query contract) is pinned by ``test_jellyfin_views.py`` /
``test_jellyfin_switchfin_surface.py``; this file covers the PURE pieces
the extraction made independently testable:

  - the ``format=Webp`` verdict (Switchfin's spellings);
  - ``as_webp``: real JPEG → WebP transcode (with maxWidth), decode-error
    fallback to the original bytes, memoization;
  - ``placeholder_avatar``: real WebP/PNG transparent bytes per format.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image

from cs_uk_api.jellyfin import images


def _jpeg_bytes(width: int = 64, height: int = 40) -> bytes:
    with io.BytesIO() as out:
        Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="JPEG")
        return out.getvalue()


# --- format verdict -------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["Webp", "webp", "/webp", "webp,kwebp"])
def test_wants_webp_accepts_switchfin_spellings(fmt: str) -> None:
    assert images.wants_webp(fmt)


@pytest.mark.parametrize("fmt", [None, "", "jpeg", "png", "webpx"])
def test_wants_webp_rejects_other_formats(fmt: Any) -> None:
    assert not images.wants_webp(fmt)


# --- webp transcode --------------------------------------------------------------


def test_as_webp_transcodes_jpeg_and_resizes() -> None:
    original = _jpeg_bytes(3200, 2000)
    out = images.as_webp("unit:test-transcode", original, 325)

    assert out.startswith(b"RIFF")  # WebP container
    assert out != original
    with Image.open(io.BytesIO(out)) as img:
        assert img.format == "WEBP"
        assert max(img.size) <= 325


def test_as_webp_falls_back_to_original_on_decode_error() -> None:
    garbage = b"\x00not-an-image"
    assert images.as_webp("unit:test-garbage", garbage, None) == garbage


def test_as_webp_memoizes_per_poster_and_width() -> None:
    url = "unit:test-memo"
    original = _jpeg_bytes()
    first = images.as_webp(url, original, None)
    second = images.as_webp(url, original, None)
    assert first is second  # same object → served from the memo
    # A different width is a different memo entry.
    third = images.as_webp(url, original, 10)
    assert third is not first


# --- placeholder avatar -----------------------------------------------------------


def test_placeholder_avatar_serves_real_webp_when_requested() -> None:
    body, ctype = images.placeholder_avatar("Webp")
    assert ctype == "image/webp"
    assert body.startswith(b"RIFF")


def test_placeholder_avatar_serves_png_by_default() -> None:
    body, ctype = images.placeholder_avatar(None)
    assert ctype == "image/png"
    assert body.startswith(b"\x89PNG")


def test_placeholder_avatar_is_transparent_one_pixel() -> None:
    body, _ = images.placeholder_avatar("png")
    with Image.open(io.BytesIO(body)) as img:
        assert img.size == (1, 1)
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 0  # fully transparent alpha


def test_placeholder_avatar_memoizes_per_format() -> None:
    webp_first = images.placeholder_avatar("webp,kwebp")
    webp_second = images.placeholder_avatar("Webp")
    assert webp_first[0] is webp_second[0]
