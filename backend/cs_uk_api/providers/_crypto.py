"""Shared AES-256-CBC + PBKDF2-HMAC-SHA512 helper.

UASerialsPro stores its player config inside
``<player-control data-tag1='{"ciphertext":...,"salt":...,"iv":...}'>``
as a crypto-js-style blob: AES-256-CBC with a 32-byte key derived
via PBKDF2-HMAC-SHA512 (999 iterations) from a hard-coded upstream
password and the hex-decoded ``salt``.

This module isolates the dependency on ``pycryptodome`` and the
raw byte-manipulation so provider modules stay focused on HTML
parsing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, cast

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .base import ProviderError


def decrypt_player_data(
    data_tag1: str, password: str, iterations: int = 999
) -> list[dict[str, Any]]:
    """AES-decrypt a ``<player-control data-tag1='...'>`` blob.

    Returns the parsed JSON list of player-tab records. Raises
    ``ProviderError`` (``code="parse_failed"``) for malformed
    payloads so the caller can surface an explicit error.
    """
    try:
        payload = json.loads(data_tag1)
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", "data-tag1 not JSON") from e
    salt_hex = payload.get("salt", "")
    iv_hex = payload.get("iv", "")
    ct_b64 = payload.get("ciphertext", "")
    if not (salt_hex and iv_hex and ct_b64):
        raise ProviderError("parse_failed", "data-tag1 missing fields")
    try:
        salt = bytes.fromhex(salt_hex)
        iv = bytes.fromhex(iv_hex)
        ct = base64.b64decode(ct_b64)
    except (ValueError, binascii.Error) as e:
        raise ProviderError("parse_failed", "data-tag1 bytes bad") from e
    # PBKDF2-HMAC-SHA512 with ``iterations`` rounds, 32-byte derived
    # key (mirrors the upstream Kotlin SecretKeyFactory
    # PBKDF2WithHmacSHA512).
    key = hashlib.pbkdf2_hmac(
        "sha512",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=32,
    )
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plain = unpad(cipher.decrypt(ct), 16)
    except (ValueError, KeyError) as e:
        raise ProviderError("parse_failed", "AES decrypt failed") from e
    text = plain.decode("utf-8", errors="replace").replace("\\", "")
    # Upstream trims after the last `]` to drop any trailing garbage
    # that the upstream serializer may have appended.
    last_bracket = text.rfind("]")
    if last_bracket != -1:
        text = text[: last_bracket + 1]
    try:
        return cast(list[dict[str, Any]], json.loads(text))
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", "decrypted data not JSON") from e


__all__ = ["decrypt_player_data"]