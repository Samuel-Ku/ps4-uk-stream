"""MoonAnime player conversation (animeon's moonanime.art upstream).

ONE owner for the moon player dialect: the obfuscated-Playerjs decode
chain (the upstream Kotlin's ``moonOuterDecode`` + ``moonDecrypt``,
stdlib-only reimplementations) and the iframe-page resolution that
turns a ``moonanime.art`` iframe URL into its final ``.m3u8`` manifest.

Import direction: this helper -> providers.base (the shared error
vocabulary) only — never the adapter. The adapter lends its fetch path
(``get_html`` — canonical error codes + ADR-0005 allowlist) and the
headers at call time; a payload-shape change on moonanime.art touches
exactly this file.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

import httpx

from .base import ProviderError

#: Playerjs iframe's obfuscation payload:
#:   atob("...==")                            (outer, moon_outer_decode)
#:   var k = "..."                            (inner XOR key)
#:   _0xd("...")                              (inner XOR, moon_decrypt)
_ATOB_RE = re.compile(r"""atob\(\s*["']([A-Za-z0-9+/=]+)["']\s*\)""")
_KEY_RE = re.compile(r"""var\s+k\s*=\s*["']([^"']+)["']""")
_INNER_RE = re.compile(r"""_0xd\s*\(\s*["']([^"']+)["']\s*\)""")


def moon_outer_decode(blob: str) -> bytes:
    """Reimplementation of the upstream Kotlin ``moonOuterDecode``.

    The Playerjs ``atob("...")`` payload is base64 of::

        [state0:u8][key:32 bytes][data:N bytes]

    Each data byte is XORed with ``key[i % 32] ^ state`` and the
    state is updated to ``(data[i] + key[i % 32]) & 0xFF``. The result
    is the JavaScript body that contains ``var k`` and ``_0xd`` calls.
    """
    raw = base64.b64decode(blob)
    if len(raw) < 33:
        return b""
    state = raw[0]
    key = raw[1:33]
    data = raw[33:]
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        k = key[i % 32]
        out[i] = (byte ^ k ^ state) & 0xFF
        state = (byte + k) & 0xFF
    return bytes(out)


def moon_decrypt(blob: str, xor_key: str) -> str:
    """Reimplementation of the upstream Kotlin ``moonDecrypt``.

    The inner cipher is base64-decode + cyclic XOR with the key string.
    The decoded text usually is a URL or a JSON snippet; failures are
    swallowed (returns ``""``) because the upstream Kotlin does the
    same.
    """
    try:
        raw = base64.b64decode(blob)
    except (ValueError, binascii.Error):
        return ""
    out = bytearray(len(raw))
    keys = [ord(c) for c in xor_key]
    for i, byte in enumerate(raw):
        out[i] = (byte ^ keys[i % len(keys)]) & 0xFF
    return out.decode("utf-8", errors="ignore")


async def resolve_moon_iframe(
    iframe_url: str,
    http: httpx.AsyncClient,
    *,
    get_html: Any,
    headers: dict[str, str],
) -> str:
    """Fetch the MoonAnime iframe page, decode the obfuscated
    Playerjs config, and return the first ``.m3u8`` URL.

    ``get_html`` is the composing provider's fetch path (canonical
    error codes + the ADR-0005 allowlist); ``headers`` are the request
    headers the adapter builds (the ``X-Requested-With`` quirk is the
    upstream Kotlin's).

    The decrypted payload is either a direct manifest URL or a JSON
    array of tracks (live 2026-08-09: movies — e.g. animeon 8102
    "Ґінтама Фільм 1" — now answer ``[{...,"file":"<m3u8>"}]``;
    the array previously meant the card was dead, today it is the
    current upstream shape).
    """
    clean = iframe_url.rstrip("?")
    if "player=" not in clean:
        separator = "&" if "?" in clean else "?"
        fetch_url = f"{clean}{separator}player=animeon.club"
    else:
        fetch_url = clean
    page = await get_html(
        fetch_url,
        http,
        headers=headers,
    )

    atob_match = _ATOB_RE.search(page)
    if not atob_match:
        raise ProviderError("parse_failed", "moon atob blob missing")
    decoded_js = moon_outer_decode(atob_match.group(1)).decode(
        "utf-8", errors="ignore"
    )
    if not decoded_js:
        raise ProviderError("parse_failed", "moon outer decode failed")

    key_match = _KEY_RE.search(decoded_js)
    if not key_match:
        raise ProviderError("parse_failed", "moon xor key missing")
    xor_key = key_match.group(1)

    for inner in _INNER_RE.findall(decoded_js):
        decoded = moon_decrypt(inner, xor_key).strip().rstrip(",")
        if decoded.startswith("["):
            try:
                tracks = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            if isinstance(tracks, list) and not tracks:
                # A well-formed EMPTY track array is deliberate
                # upstream unavailability — the movie is listed in
                # the catalog but moonanime hasn't published the
                # video yet (live 2026-08-09: animeon 8104
                # «Літературне дівча Фільм» serves a "Скоро
                # доступно" placeholder iframe and an empty `[]`
                # player payload). Per ADR-0002's empty-manifest
                # amendment this is `gated` (client 404, never a
                # health signal), NOT `parse_failed` (502, pollutes
                # the health tracker for a healthy provider).
                raise ProviderError(
                    "gated", "no playable tracks — video not yet published"
                )
            for track in tracks if isinstance(tracks, list) else []:
                url = str(track.get("file") or "").strip()
                if ".m3u8" in url:
                    return url
            continue
        if ".m3u8" not in decoded:
            continue
        return decoded
    raise ProviderError("parse_failed", "no .m3u8 in moon payload")