"""Unit tests for the extracted HLS byte-proxy module (ticket #342).

The route-level behaviour (respx-driven byte/manifest proxying) is pinned
by ``test_jellyfin_stream.py``; this file covers the PURE pieces the
extraction made independently testable:

  - ``_rewrite_m3u8`` given a manifest text: plain segment lines,
    ``URI="..."`` attributes, child playlists, blank-line preservation;
  - host-guard verdicts: ``_registrable_domain`` and
    ``_stream_target_allowed`` (sibling subdomains pass, foreign
    registrable domains fail closed unless provider-sanctioned);
  - the per-item header memo: fresh-hit serves a COPY without calling the
    resolver; a stale entry falls back to one re-resolution.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from cs_uk_api.jellyfin import hls_proxy
from cs_uk_api.models import StreamResponse

_GK = "g2:abcdefabcdefabcdef"


# --- m3u8 rewrite --------------------------------------------------------------


def test_rewrite_resolves_relative_segments_against_manifest_url() -> None:
    body = "#EXTM3U\n#EXTINF:6.0,\nseg000.ts\n"
    out = hls_proxy._rewrite_m3u8(body, "https://cdn.example.test/hls/master.m3u8", _GK)
    expected_seg = (
        f"/Videos/{_GK}/segment?url="
        f"https%3A%2F%2Fcdn.example.test%2Fhls%2Fseg000.ts"
    )
    assert out == f"#EXTM3U\n#EXTINF:6.0,\n{expected_seg}\n"


def test_rewrite_keeps_absolute_references_verbatim() -> None:
    body = "#EXTM3U\nhttps://other.cdn.example.test/a/seg001.ts\n"
    out = hls_proxy._rewrite_m3u8(body, "https://cdn.example.test/master.m3u8", _GK)
    assert (
        f"/Videos/{_GK}/segment?url="
        f"https%3A%2F%2Fother.cdn.example.test%2Fa%2Fseg001.ts" in out
    )
    # No double-scheme mangling: the absolute URL rides inside the query.
    assert "httpscdn.example.test" not in out


def test_rewrite_uri_attributes_keys_and_maps() -> None:
    body = (
        '#EXT-X-KEY:METHOD=AES-128,URI="caption.key"\n'
        '#EXT-X-MAP:URI="init.mp4"\n'
        "seg000.ts\n"
    )
    out = hls_proxy._rewrite_m3u8(body, "https://cdn.example.test/hls/m.m3u8", _GK)
    key = f"/Videos/{_GK}/segment?url=https%3A%2F%2Fcdn.example.test%2Fhls%2Fcaption.key"
    init = f"/Videos/{_GK}/segment?url=https%3A%2F%2Fcdn.example.test%2Fhls%2Finit.mp4"
    assert f'URI="{key}"' in out
    assert f'URI="{init}"' in out
    # No raw upstream URI escapes the rewrite.
    assert 'URI="caption.key"' not in out


def test_rewrite_preserves_blank_lines_and_trailing_newline() -> None:
    body = "#EXTM3U\n\n\n#EXT-X-ENDLIST\n"
    out = hls_proxy._rewrite_m3u8(body, "https://cdn.example.test/m.m3u8", _GK)
    lines = out.split("\n")
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "" and lines[2] == ""
    assert lines[-1] == ""  # join adds exactly one trailing newline


def test_segment_url_is_fully_percent_encoded() -> None:
    url = hls_proxy._segment_url(_GK, "https://cdn.example.test/a b?s=1")
    assert url.startswith(f"/Videos/{_GK}/segment?url=")
    assert " " not in url and "?" not in url.split("url=", 1)[1]


# --- host guard ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.test", "example.test"),  # already two labels
        ("api.unimay.media", "unimay.media"),
        ("a.b.c.example.test", "example.test"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert hls_proxy._registrable_domain(host) == expected


def test_guard_sibling_subdomain_of_same_registrable_domain_passes() -> None:
    assert hls_proxy._stream_target_allowed(
        "https://media.cdn.example.test/s.ts", "cdn.example.test"
    )


def test_guard_foreign_host_fails_closed() -> None:
    assert not hls_proxy._stream_target_allowed("https://evil.example/steal.ts", "cdn.example.test")


def test_guard_provider_sanctioned_domain_passes() -> None:
    assert hls_proxy._stream_target_allowed(
        "https://dl.dropboxusercontent.test/f.mp4",
        "gateway.example.test",
        frozenset({"dropboxusercontent.test"}),
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://cdn.example.test/s.ts",
        "//cdn.example.test/s.ts",
        "/relative/only.ts",
    ],
)
def test_guard_non_http_schemes_fail_closed(url: str) -> None:
    assert not hls_proxy._stream_target_allowed(url, "cdn.example.test")


def test_hls_detection_covers_type_and_extension() -> None:
    def stream(stream_type: str, url: str) -> Any:
        return StreamResponse(url=url, type=stream_type)

    assert hls_proxy._is_hls_stream(stream("m3u8", "https://x.test/m"))
    assert hls_proxy._is_hls_stream(stream("hls", "https://x.test/m"))
    assert hls_proxy._is_hls_stream(stream("mp4", "https://x.test/m.m3u8"))
    assert not hls_proxy._is_hls_stream(stream("mp4", "https://x.test/v.mp4"))


# --- per-item header memo -------------------------------------------------------


def _memo_hit(cdn: str) -> None:
    hls_proxy._memo_stream(_GK, cdn, {"Referer": "https://cdn.example.test/r"}, frozenset())


@pytest.fixture(autouse=True)
def _clean_memo() -> Any:
    hls_proxy._STREAM_MEMO.clear()
    yield
    hls_proxy._STREAM_MEMO.clear()


def test_memo_fresh_hit_returns_copy_without_resolution() -> None:
    _memo_hit("cdn.example.test")

    async def _fail(item_id: str) -> StreamResponse | None:
        raise AssertionError("resolver must not run on a fresh memo hit")

    target = asyncio.run(hls_proxy.segment_target(_GK, resolve_stream=_fail))
    assert target is not None
    cdn, headers, allowed = target
    assert cdn == "cdn.example.test"
    assert headers == {"Referer": "https://cdn.example.test/r"}
    assert allowed == frozenset()
    # A copy, never the live dict: mutating the answer cannot poison the memo.
    headers["Referer"] = "mutated"
    assert hls_proxy._STREAM_MEMO[_GK][2]["Referer"] == "https://cdn.example.test/r"


def test_memo_stale_entry_re_resolves_once() -> None:
    _memo_hit("stale.example.test")
    ts, _, _, allowed = hls_proxy._STREAM_MEMO[_GK]
    hls_proxy._STREAM_MEMO[_GK] = (ts - hls_proxy._STREAM_MEMO_TTL_S - 1, "stale.example.test", {}, allowed)

    calls: list[str] = []

    async def _resolve(item_id: str) -> StreamResponse | None:
        calls.append(item_id)
        return StreamResponse(
            url="https://fresh.example.test/v.mp4",
            type="mp4",
            headers={"Referer": "https://fresh.example.test/r"},
        )

    target = asyncio.run(hls_proxy.segment_target(_GK, resolve_stream=_resolve))
    assert calls == [_GK]
    assert target is not None
    assert target[0] == "fresh.example.test"
    # ...and the fresh verdict was written back over the stale entry.
    assert hls_proxy._STREAM_MEMO[_GK][1] == "fresh.example.test"


def test_memo_unresolvable_id_yields_none() -> None:
    async def _none(item_id: str) -> StreamResponse | None:
        return None

    assert asyncio.run(hls_proxy.segment_target(_GK, resolve_stream=_none)) is None


def test_time_monotonic_is_the_ttl_clock() -> None:
    """The memo compares against ``time.monotonic`` (immune to wall-clock
    jumps) — pin the invariant by writing a monotonic-fresh entry."""
    _memo_hit("cdn.example.test")
    ts = hls_proxy._STREAM_MEMO[_GK][0]
    assert 0 <= time.monotonic() - ts < hls_proxy._STREAM_MEMO_TTL_S
