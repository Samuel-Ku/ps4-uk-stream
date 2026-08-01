"""Tests for gate tooling helpers (issue #30, grilling Q2/Q4).

The pure logic of the live gate lives in ``cs_uk_api/gate_tools`` so the
bash wrapper ``scripts/gate.sh`` stays thin and the decision rules are
unit-testable.
"""
from __future__ import annotations

from cs_uk_api.gate_tools import PlayabilityProfile, parse_ffprobe, scan_js_markers

# ---------- JS marker scan (Q2: diagnostic mode) ----------


def test_scan_detects_eval_call():
    html = "<script>var x = eval('atob(\"aGk=\")');</script>"
    markers = scan_js_markers(html)
    assert "eval(" in markers
    assert "atob(" in markers


def test_scan_detects_function_constructor():
    html = "const f = new Function('return 1');"
    assert "Function(" in scan_js_markers(html)


def test_scan_detects_obfuscated_marker():
    html = "// obfuscated by packer"
    assert "obfuscated" in scan_js_markers(html)


def test_scan_case_insensitive_obfuscation():
    html = "Obfuscated.stream"
    assert "obfuscated" in scan_js_markers(html)


def test_scan_clean_html_returns_empty():
    html = "<html><body><div class='short-t'>Фільм</div></body></html>"
    assert scan_js_markers(html) == []


def test_scan_markers_do_not_false_positive_on_evaluate_keyword():
    html = "evaluation = 1; functionality"
    assert scan_js_markers(html) == []


# ---------- ffprobe playability profile (Q4) ----------


def test_parse_ffprobe_h264_is_not_soft_decode_risk():
    payload = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "bit_rate": "2500000"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"bit_rate": "2800000"},
    }
    p = parse_ffprobe(payload)
    assert p.codec == "h264"
    assert p.width == 1920
    assert p.height == 1080
    assert p.bitrate_kbps == 2500
    assert p.soft_decode_risk is False


def test_parse_ffprobe_av1_is_soft_decode_risk():
    payload = {
        "streams": [{"codec_type": "video", "codec_name": "av1", "width": 3840, "height": 2160}],
        "format": {"bit_rate": "8000000"},
    }
    p = parse_ffprobe(payload)
    assert p.codec == "av1"
    assert p.soft_decode_risk is True


def test_parse_ffprobe_falls_back_to_format_bitrate():
    payload = {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}],
        "format": {"bit_rate": "1500000"},
    }
    p = parse_ffprobe(payload)
    assert p.bitrate_kbps == 1500
    assert p.soft_decode_risk is False


def test_parse_ffprobe_no_video_stream_is_risk_unknown():
    payload = {"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}}
    p = parse_ffprobe(payload)
    assert p.codec is None
    assert p.soft_decode_risk is True  # unknown -> cannot trust it on PS4


def test_parse_ffprobe_missing_bitrate_is_none():
    payload = {"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {}}
    p = parse_ffprobe(payload)
    assert p.bitrate_kbps is None


def test_parse_ffprobe_missing_streams_key():
    p = parse_ffprobe({})
    assert p.codec is None
    assert p.soft_decode_risk is True


def test_profile_string_repr_compact():
    p = PlayabilityProfile(codec="h264", width=1920, height=1080, bitrate_kbps=2500)
    assert "h264" in str(p)
    assert "1920x1080" in str(p)
    assert "2500" in str(p)


def test_profile_default_no_risk():
    p = PlayabilityProfile(codec="h264")
    assert p.soft_decode_risk is False
