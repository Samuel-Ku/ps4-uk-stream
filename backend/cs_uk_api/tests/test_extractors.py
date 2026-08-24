"""Tests for the shared extractor layer (issue #8)."""
from __future__ import annotations

from cs_uk_api.extractors import (
    ExtractResult,
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


# ---------- module exports ----------


def test_module_exports_expected_symbols():
    from cs_uk_api import extractors

    assert hasattr(extractors, "ExtractResult")
    assert hasattr(extractors, "BaseExtractor")
    assert hasattr(extractors, "RegexExtractor")
    assert hasattr(extractors, "walk_playlist")
