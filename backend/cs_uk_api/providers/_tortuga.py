"""Shared Tortuga player XOR/Base64 decoder.

The Tortuga player page (hosted at ``tortuga.tw``) embeds an obfuscated
``file:`` payload that the upstream Kotlin source resolves via
``Decoder.tortugaDecode`` (and its sibling ``Decoder.torDecrypt``).
Both routines share the same XOR core:

    salt = first decoded byte
    for i = 1, 2, ...:
        key = (salt + 7*(i-1) + 13) % 256
        out[i-1] = decoded[i] XOR key

…and differ only in how they pre-clean the base64 string. This
module exposes the canonical ``tortugaDecode`` variant (Android
``Base64.DEFAULT`` lenient padding) so the three providers that hit
``tortuga.tw`` (uaserialspro, serialno, kinovezha) share one
implementation.
"""

from __future__ import annotations

import base64
import binascii


def tortuga_decode(payload: str) -> str:
    """Mirror the upstream Kotlin ``Decoder.tortugaDecode``.

    1. Strip trailing ``=`` and re-pad to a multiple of 4 (Android's
       ``Base64.DEFAULT`` is lenient about trailing padding).
    2. Base64-decode. The first byte is the salt; every subsequent
       byte is XORed with ``(salt + 7*i + 13) % 256`` for i = 0, 1, …
    3. UTF-8 decode the resulting bytes, replacing malformed
       sequences rather than raising — Tortuga occasionally
       smuggles stray bytes in the tail.

    Returns an empty string on bad input so callers can surface
    ``parse_failed`` without catching exceptions.
    """
    if not payload:
        return ""
    clean = payload.rstrip("=")
    pad = (4 - len(clean) % 4) % 4
    try:
        decoded = base64.b64decode(clean + "=" * pad)
    except (ValueError, binascii.Error):
        return ""
    if len(decoded) < 2:
        return ""
    salt = decoded[0]
    out = bytearray(len(decoded) - 1)
    for i in range(1, len(decoded)):
        key = (salt + 7 * (i - 1) + 13) % 256
        out[i - 1] = decoded[i] ^ key
    return out.decode("utf-8", errors="replace")


__all__ = ["tortuga_decode"]