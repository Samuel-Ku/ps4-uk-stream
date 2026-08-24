"""Walker matrix tests for extractors.playlist.walk_playlist (spec #361 batch 1)."""
from __future__ import annotations

from cs_uk_api.extractors.playlist import walk_playlist


def test_flat_seasons_serialno_live():
    payload = [
        {"folder": [{"title": "Серія 1", "file": "https://cdn.example/s1e1.m3u8"}, {"title": "Серія 2", "file": "https://cdn.example/s1e2.m3u8"}]},
        {"folder": [{"title": "Серія 1", "file": "https://cdn.example/s2e1.m3u8"}]},
    ]
    assert walk_playlist(payload, 1, 1) == "https://cdn.example/s1e1.m3u8"
    assert walk_playlist(payload, 1, 2) == "https://cdn.example/s1e2.m3u8"
    assert walk_playlist(payload, 2, 1) == "https://cdn.example/s2e1.m3u8"


def test_flat_with_dub_prefix_and_subtitle_stripping():
    payload = [
        {"folder": [{"title": "Серія 1", "file": "{OZZ}https://cdn.example/s1e1.m3u8(subtitle:https://sub.vtt)"}]},
    ]
    assert walk_playlist(payload, 1, 1) == "https://cdn.example/s1e1.m3u8"
    # translation match
    assert walk_playlist(payload, 1, 1, translation="OZZ") == "https://cdn.example/s1e1.m3u8"
    # unknown translation falls back to index
    assert walk_playlist(payload, 1, 1, translation="UNKNOWN") == "https://cdn.example/s1e1.m3u8"


def test_dub_wrapped_klontv_top_level_title_match_and_fallback():
    payload = [
        {"title": "BAMBUA", "folder": [{"title": " Сезон 1", "folder": [{"title": "Серія 1", "file": "https://cdn.example/bamboo-s1e1.m3u8"}]}]},
        {"title": "OTHER", "folder": [{"title": " Сезон 1", "folder": [{"title": "Серія 1", "file": "https://cdn.example/other-s1e1.m3u8"}]}]},
    ]
    assert walk_playlist(payload, 1, 1, translation="BAMBUA") == "https://cdn.example/bamboo-s1e1.m3u8"
    assert walk_playlist(payload, 1, 1, translation="OTHER") == "https://cdn.example/other-s1e1.m3u8"
    # fallback to first dub when translation missing
    assert walk_playlist(payload, 1, 1) == "https://cdn.example/bamboo-s1e1.m3u8"
    assert walk_playlist(payload, 1, 1, translation="MISSING") == "https://cdn.example/bamboo-s1e1.m3u8"


def test_season_top_dub_inside_eneyida():
    payload = [
        {"title": "1 сезон", "folder": [{"title": "DubA", "folder": [{"title": "Серія 1", "file": "https://cdn.example/duba-s1e1.m3u8"}, {"title": "Серія 2", "file": "https://cdn.example/duba-s1e2.m3u8"}]}, {"title": "DubB", "folder": [{"title": "Серія 1", "file": "https://cdn.example/dubb-s1e1.m3u8"}]}]},
        {"title": "2 сезон", "folder": [{"title": "DubA", "folder": [{"title": "Серія 1", "file": "https://cdn.example/duba-s2e1.m3u8"}]}]},
    ]
    assert walk_playlist(payload, 1, 1, translation="DubA") == "https://cdn.example/duba-s1e1.m3u8"
    assert walk_playlist(payload, 1, 1, translation="DubB") == "https://cdn.example/dubb-s1e1.m3u8"
    assert walk_playlist(payload, 1, 2, translation="DubA") == "https://cdn.example/duba-s1e2.m3u8"
    assert walk_playlist(payload, 2, 1) == "https://cdn.example/duba-s2e1.m3u8"
    # wrapper shape: single outer entry whose folder holds seasons
    wrapper = [{"title": "wrapper", "folder": payload}]
    assert walk_playlist(wrapper, 1, 1, translation="DubA") == "https://cdn.example/duba-s1e1.m3u8"


def test_bare_movie_http_file():
    assert walk_playlist("https://cdn.example/movie.m3u8", 1, 1) == "https://cdn.example/movie.m3u8"
    assert walk_playlist("{OZZ}https://cdn.example/movie.m3u8(subtitle:https://sub.vtt)", 1, 1) == "https://cdn.example/movie.m3u8"
    assert walk_playlist("https://cdn.example/movie.m3u8", 99, 99) == "https://cdn.example/movie.m3u8"


def test_out_of_range_returns_none():
    payload = [
        {"folder": [{"title": "Серія 1", "file": "https://cdn.example/s1e1.m3u8"}]},
    ]
    assert walk_playlist(payload, 2, 1) is None
    assert walk_playlist(payload, 1, 2) is None
    assert walk_playlist(payload, 0, 1) is None


def test_malformed_and_empty_payload_returns_none():
    assert walk_playlist([], 1, 1) is None
    assert walk_playlist(None, 1, 1) is None  # type: ignore[arg-type]
    assert walk_playlist([{"no_folder": 1}], 1, 1) is None
    assert walk_playlist("not a url", 1, 1) is None
    assert walk_playlist("", 1, 1) is None
