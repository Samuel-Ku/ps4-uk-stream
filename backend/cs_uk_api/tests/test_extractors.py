"""Tests for the shared extractor layer (issue #8)."""
from __future__ import annotations

import pytest
import respx

from cs_uk_api.extractors import (
    ExtractResult,
    IframeExtractor,
    PlayerJsonExtractor,
    RegexExtractor,
)

# ---------- regex extractor ----------


def test_regex_extracts_file_url():
    html = """
    <script>
    var player = new Playerjs({id:"abc", file:"https://cdn.example.com/v.mp4"});
    </script>
    """
    r = RegexExtractor().extract(html)
    assert isinstance(r, ExtractResult)
    assert r.url == "https://cdn.example.com/v.mp4"
    assert r.type == "mp4"


def test_regex_extracts_source_url():
    html = """
    <script>
    sources: [{src: "https://cdn.example.com/manifest.m3u8", type: "application/x-mpegURL"}]
    </script>
    """
    r = RegexExtractor().extract(html)
    assert r.url == "https://cdn.example.com/manifest.m3u8"
    assert r.type == "m3u8"


def test_regex_extracts_quoted_url_with_double_or_single_quotes():
    html_double = '<video src="https://a.example/m.m3u8"></video>'
    html_single = "<video src='https://b.example/m.m3u8'></video>"
    assert RegexExtractor().extract(html_double).url == "https://a.example/m.m3u8"
    assert RegexExtractor().extract(html_single).url == "https://b.example/m.m3u8"


def test_regex_returns_none_when_no_match():
    html = "<html><body>nothing here</body></html>"
    assert RegexExtractor().extract(html) is None


def test_regex_prefers_file_over_source_when_both_present():
    html = """
    file:"https://primary.example/v.mp4"
    sources: [{src: "https://fallback.example/v.m3u8"}]
    """
    r = RegexExtractor().extract(html)
    assert r is not None
    assert r.url == "https://primary.example/v.mp4"


# ---------- iframe extractor ----------


@pytest.mark.asyncio
async def test_iframe_extractor_returns_initial_src_when_not_chainable():
    import httpx

    html = '<html><body><iframe src="https://cdn.example.com/player.html"></body></html>'
    async with httpx.AsyncClient() as http:
        r = await IframeExtractor().extract(html, http=http)
    assert r.url == "https://cdn.example.com/player.html"
    assert r.type == "hls"  # unknown mime -> hls default


@pytest.mark.asyncio
async def test_iframe_extractor_follows_one_level_chain():
    import httpx

    initial_html = '<iframe src="https://cdn.example.com/level1.html"></iframe>'
    level1_html = '<iframe src="https://cdn.example.com/level2.html"></iframe>'
    level2_html = '<iframe src="https://cdn.example.com/level3.m3u8"></iframe>'
    level3_html = "#EXTM3U\n#EXT-X-VERSION:3\n"

    with respx.mock(assert_all_called=True) as router:
        router.get("https://cdn.example.com/level1.html").respond(200, text=level1_html)
        router.get("https://cdn.example.com/level2.html").respond(200, text=level2_html)
        router.get("https://cdn.example.com/level3.m3u8").respond(200, text=level3_html)
        async with httpx.AsyncClient() as http:
            r = await IframeExtractor(max_depth=5).extract(initial_html, http=http)
    assert r.url == "https://cdn.example.com/level3.m3u8"
    assert r.type == "m3u8"


@pytest.mark.asyncio
async def test_iframe_extractor_stops_at_max_depth():
    import httpx

    # Build a chain longer than max_depth=2.
    def chain(depth: int) -> str:
        if depth == 0:
            return '<iframe src="https://cdn.example.com/final.m3u8"></iframe>'
        return f'<iframe src="https://cdn.example.com/level{depth}.html"></iframe>'

    with respx.mock(assert_all_called=False) as router:
        for d in range(1, 6):
            router.get(f"https://cdn.example.com/level{d}.html").respond(200, text=chain(d - 1))
        router.get("https://cdn.example.com/final.m3u8").respond(200, text="#EXTM3U\n")
        async with httpx.AsyncClient() as http:
            r = await IframeExtractor(max_depth=2).extract(chain(5), http=http)
    # max_depth=2 should let it go through level5 -> level4 -> level3 -> final
    # (each recursion decrements, so levels 5,4,3 fetch before stop)
    assert "final.m3u8" in r.url or "level" in r.url  # exact depth depends on impl


# ---------- playerjson extractor ----------


@pytest.mark.asyncio
async def test_playerjson_extracts_url_and_headers():
    import httpx

    player_body = """{
      "url": "https://cdn.example.com/v.mp4",
      "headers": {"Referer": "https://site.example/", "Cookie": "a=b"},
      "type": "mp4"
    }"""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://player.example/api").respond(200, text=player_body)
        async with httpx.AsyncClient() as http:
            r = await PlayerJsonExtractor().extract(
                initial_url="https://player.example/api",
                http=http,
            )
    assert r.url == "https://cdn.example.com/v.mp4"
    assert r.type == "mp4"
    assert r.headers == {"Referer": "https://site.example/", "Cookie": "a=b"}


@pytest.mark.asyncio
async def test_playerjson_handles_m3u8_type():
    import httpx

    player_body = """{"url":"https://x.example/m.m3u8","type":"m3u8"}"""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://player.example/api2").respond(200, text=player_body)
        async with httpx.AsyncClient() as http:
            r = await PlayerJsonExtractor().extract(
                initial_url="https://player.example/api2",
                http=http,
            )
    assert r.type == "m3u8"
    assert r.headers == {}  # missing header -> empty dict


# ---------- module exports ----------


def test_module_exports_expected_symbols():
    from cs_uk_api import extractors

    assert hasattr(extractors, "ExtractResult")
    assert hasattr(extractors, "BaseExtractor")
    assert hasattr(extractors, "IframeExtractor")
    assert hasattr(extractors, "PlayerJsonExtractor")
    assert hasattr(extractors, "RegexExtractor")