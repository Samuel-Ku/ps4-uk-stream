"""Shared Tortuga stream URL decoder.

Used by serialno, kinovezha, and uaserialspro. Their Tortuga player
``file:`` values are XOR-base64 encoded with a salt-derived byte key
(the upstream Kotlin calls these ``Decoder.tortugaDecode`` and
``Decoder.torDecrypt``).
"""
from __future__ import annotations

import base64
import binascii
import re


def decode(value: str) -> str:
    """XOR-decode a Tortuga-encoded stream URL.

    Returns the decoded string, or ``value`` unchanged if it cannot be
    decoded (callers should fall back to treating ``value`` as plain).
    """
    if not value:
        return value
    cleaned = re.sub(r"[^A-Za-z0-9+/]", "", value)
    padding = len(cleaned) % 4
    if padding > 1:
        cleaned += "=" * (4 - padding)
    try:
        raw = base64.b64decode(cleaned, validate=False)
    except (ValueError, binascii.Error):
        return value
    if len(raw) < 2:
        return value
    salt = raw[0]
    out = bytearray(len(raw) - 1)
    for i, byte in enumerate(raw[1:]):
        key = (salt + 7 * i + 13) % 256
        out[i] = byte ^ key
    return out.decode("utf-8", errors="replace")


__all__ = ["decode"]
