"""UASerialsPro AES-CBC player-data decryption helpers."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, cast

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .base import ProviderError

_PLAYER_PASSWORD = "297796CCB81D255125"
_PBKDF2_ITERATIONS = 999


def decrypt_player_data(data_tag1: str) -> list[dict[str, Any]]:
    """Decrypt and parse a UASerialsPro ``data-tag1`` player blob."""
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
    key = hashlib.pbkdf2_hmac(
        "sha512",
        _PLAYER_PASSWORD.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plain = unpad(cipher.decrypt(ct), 16)
    except (ValueError, KeyError) as e:
        raise ProviderError("parse_failed", "AES decrypt failed") from e
    text = plain.decode("utf-8", errors="replace").replace("\\", "")
    last_bracket = text.rfind("]")
    if last_bracket != -1:
        text = text[: last_bracket + 1]
    try:
        return cast(list[dict[str, Any]], json.loads(text))
    except json.JSONDecodeError as e:
        raise ProviderError("parse_failed", "decrypted data not JSON") from e


__all__ = ["decrypt_player_data"]
