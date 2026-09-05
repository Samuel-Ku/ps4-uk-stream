"""The animeon player-conversation seam (deepening: conversations out).

``providers/_moon_player`` (decryptors + iframe resolution) and
``providers/_ashdi_player`` (iframe scrape + playlist fallback) are the
animeon adapter's player conversations, one module per upstream. These
tests pin the moon decode chain DIRECTLY — the decryptors are pure
functions (the interface is the test surface; previously testable only
through the whole provider) — plus the ashdi playlist parser.
"""

from __future__ import annotations

import base64

from cs_uk_api.providers._ashdi_player import resolve_playlist_page
from cs_uk_api.providers._moon_player import moon_decrypt, moon_outer_decode

# ---------------------------------------------------------------------------
# moon decode chain (pure, ported from the upstream Kotlin)
# ---------------------------------------------------------------------------


def _moon_payload(xor_key: bytes, js_body: bytes) -> str:
    """Build the upstream's outer payload: [state0][key:32][data].

    The cipher advances its state on the CIPHERTEXT byte (the decoder's
    state update reads the raw data region), so the encoder mirrors
    that: c = p ^ k ^ state; state = (c + k) & 0xFF.
    """
    assert len(xor_key) == 32
    state = 0
    data = bytearray()
    for i, byte in enumerate(js_body):
        k = xor_key[i % 32]
        c = (byte ^ k ^ state) & 0xFF
        data.append(c)
        state = (c + k) & 0xFF
    return base64.b64encode(bytes([0]) + xor_key + bytes(data)).decode("ascii")


def test_moon_outer_decode_round_trips_js_body() -> None:
    key = bytes(range(32))
    body = b'var k = "abc"; _0xd("Zm9v"); player().ready'
    decoded = moon_outer_decode(_moon_payload(key, body))
    assert decoded == body


def test_moon_outer_decode_short_payload_is_empty() -> None:
    assert moon_outer_decode(base64.b64encode(b"short").decode()) == b""


def test_moon_decrypt_round_trips_base64_xor() -> None:
    """Encode (XOR then b64) then decode must round-trip."""
    text = "https://cdn.example/video.m3u8"
    key = "KEY"
    raw = bytearray()
    for i, byte in enumerate(text.encode()):
        raw.append(byte ^ ord(key[i % len(key)]))
    blob = base64.b64encode(bytes(raw)).decode()
    assert moon_decrypt(blob, key) == text


def test_moon_decrypt_bad_base64_swallows() -> None:
    assert moon_decrypt("!!!not-base64!!!", "K") == ""


# ---------------------------------------------------------------------------
# ashdi playlist parser (pure page → entry walk)
# ---------------------------------------------------------------------------

_PLAYLIST_PAGE = (
    "Playerjs({"
    "file:'["
    "{\"title\":\"Norma\",\"folder\":["
    "{\"title\":\"Сезон 1\",\"folder\":["
    "{\"title\":\"Серія 1\",\"file\":\"https://ashdi.vip/s1e1.m3u8\"},"
    "{\"title\":\"Серія 2\",\"file\":\"https://ashdi.vip/s1e2.m3u8\"}"
    "]}]},"
    "{\"title\":\"Оригінал\",\"folder\":["
    "{\"title\":\"Сезон 1\",\"folder\":["
    "{\"title\":\"Серія 1\",\"file\":\"https://ashdi.vip/orig1.m3u8\"}"
    "]}]}"
    "]'});"
)


def test_playlist_parser_selects_wanted_translation() -> None:
    url = resolve_playlist_page(
        _PLAYLIST_PAGE, translation_name="оригінал", episode_num=1
    )
    assert url == "https://ashdi.vip/orig1.m3u8"
    url = resolve_playlist_page(
        _PLAYLIST_PAGE, translation_name="NORMA", episode_num=2
    )
    assert url == "https://ashdi.vip/s1e2.m3u8"


def test_playlist_parser_no_name_match_takes_first_folder() -> None:
    url = resolve_playlist_page(
        _PLAYLIST_PAGE, translation_name="нетакий", episode_num=1
    )
    assert url == "https://ashdi.vip/s1e1.m3u8"


def test_playlist_parser_missing_entry_is_none() -> None:
    assert (
        resolve_playlist_page(
            _PLAYLIST_PAGE, translation_name="norma", episode_num=99
        )
        is None
    )
    assert resolve_playlist_page("<html>no player here</html>", translation_name="x", episode_num=1) is None


def test_playlist_parser_bad_json_is_none() -> None:
    assert (
        resolve_playlist_page("file:'not json'", translation_name="x", episode_num=1)
        is None
    )
