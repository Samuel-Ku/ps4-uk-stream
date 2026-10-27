"""Unit pins for ``scripts/teleport_calibrate.py``.

The live route is exercised by an operator run; these tests pin the pure
pieces that make that run repeatable: the wire-id grammar, the multipart
body, the ffmpeg argv, the verdict table, the local WAV guard, and the
cold-by-default request plane.

Run from the repo root: ``python3 -m pytest scripts/tests -q``.
"""

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "teleport_calibrate.py"
)
_spec = importlib.util.spec_from_file_location("teleport_calibrate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
tc = importlib.util.module_from_spec(_spec)
sys.modules["teleport_calibrate"] = tc
_spec.loader.exec_module(tc)


# --- wire ids and warm-up titles --------------------------------------


def test_content_id_strips_sxxexx_and_bare_episode_blob():
    assert tc.content_id("kinotron:9709-gra-prestolv:s1e1") == (
        "kinotron:9709-gra-prestolv"
    )
    assert tc.content_id("animeon:918:e1:eyJzb3VyY2Ui") == "animeon:918"
    assert tc.content_id("g2:abc123") == "g2:abc123"


def test_search_title_cleans_provider_suffixes():
    assert tc.search_title("Гра Престолів дивитись онлайн") == "Гра Престолів"
    assert tc.search_title("Клініка (2001)") == "Клініка"
    assert tc.search_title("Title дивитись онлайн (2020)") == "Title"
    assert tc.search_title("Тато / Daddy") == "Тато"


# --- multipart body --------------------------------------------------


def test_multipart_body_puts_fields_before_the_file():
    body = tc.multipart_body(
        [("item_id", "kinotron:1:s1e1"), ("position_ticks", "6000000000")],
        "reference-600s.wav",
        b"RIFFdata",
        "----boundary",
    )
    assert body.startswith(b"------boundary\r\n")
    assert b'name="item_id"\r\n\r\nkinotron:1:s1e1\r\n' in body
    assert b'name="position_ticks"\r\n\r\n6000000000\r\n' in body
    assert b'filename="reference-600s.wav"\r\n' in body
    assert b"Content-Type: audio/wav\r\n\r\nRIFFdata" in body
    assert body.endswith(b"\r\n------boundary--\r\n")


# --- ffmpeg argv -----------------------------------------------------


def test_ffmpeg_command_puts_headers_and_seek_before_input():
    stream = tc.StreamInfo(
        url="https://cdn.example/show/index.m3u8",
        type="m3u8",
        headers={"Referer": "https://provider.example/", "User-Agent": "test"},
    )
    command = tc.ffmpeg_command(
        "ffmpeg", stream, 600, 15, Path("/tmp/reference-600s.wav")
    )
    assert command[0] == "ffmpeg"
    assert "-nostdin" in command
    header_index = command.index("-headers")
    assert command[header_index + 1] == (
        "Referer: https://provider.example/\r\nUser-Agent: test\r\n"
    )
    assert command[command.index("-ss") + 1] == "585.000"
    assert command[command.index("-t") + 1] == "15.000"
    assert command[command.index("-i") + 1] == stream.url
    assert command[-1] == "/tmp/reference-600s.wav"


# --- verdict table / summary -----------------------------------------


def test_format_table_renders_matched_and_error_rows():
    verdicts = [
        tc.Verdict(
            position=120,
            status="matched",
            offset_seconds=-0.0061906727,
            confidence=0.9875919117,
            margin=0.3272058823,
            seek_ticks=1_199_938_000,
            reason=None,
            latency_s=13.391,
            http_status=200,
        ),
        tc.Verdict(
            position=600,
            status="error",
            offset_seconds=None,
            confidence=None,
            margin=None,
            seek_ticks=None,
            reason=None,
            latency_s=0.031,
            http_status=400,
            error="invalid_request: bad ids",
        ),
    ]
    table = tc.format_table(verdicts)
    lines = table.splitlines()
    assert lines[0].startswith("| #")
    assert "Request" in lines[0]
    assert "---:" in lines[1]
    assert "120 s" in lines[2]
    assert "matched" in lines[2]
    assert "-0.0062 s" in lines[2]
    assert "0.9876" in lines[2]
    assert "119.9938 s" in lines[2]
    assert "13.39 s" in lines[2]
    assert "error 400" in lines[3]
    assert "—" in lines[3]


def test_summarize_counts_and_ranges_ignoring_capture_latency():
    verdicts = [
        tc.Verdict(120, "matched", 0.01, 0.983, 0.34, 1_200_100_000, None, 12.0, 200),
        tc.Verdict(300, "matched", 0.02, 0.990, 0.35, 3_000_200_000, None, 15.0, 200),
        tc.Verdict(
            600,
            "error",
            None,
            None,
            None,
            None,
            None,
            0.0,
            None,
            "clip capture failed: ffmpeg",
        ),
    ]
    summary = tc.summarize(verdicts)
    assert "2/3 returned 200" in summary
    assert "matched=2" in summary
    assert "errors=1" in summary
    assert "confidence 0.9830–0.9900" in summary
    assert "margin 0.3400–0.3500" in summary
    assert "request 12.00–15.00 s" in summary


# --- error vocabulary / local clip guard -----------------------------


def test_error_message_reads_route_error_body():
    assert tc.error_message(
        {"detail": {"error": "invalid_request", "message": "bad ids"}}
    ) == "invalid_request: bad ids"
    assert tc.error_message({"detail": "plain failure"}) == "plain failure"
    assert tc.error_message({"detail": [{"loc": ["body"], "msg": "bad"}]}).startswith(
        "[{"
    )


def _write_wav(path: Path, seconds: float) -> None:
    frames = int(48_000 * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes(b"\x00\x00" * 2 * frames)


def test_validate_clip_accepts_the_route_pcm_shape(tmp_path):
    path = tmp_path / "ok.wav"
    _write_wav(path, 15)
    assert tc.validate_clip(path, 15) == pytest.approx(15.0)


def test_validate_clip_rejects_a_short_capture(tmp_path):
    path = tmp_path / "short.wav"
    _write_wav(path, 1)
    with pytest.raises(tc.CalibrationError, match="accepts 10-20s"):
        tc.validate_clip(path, 15)


def test_validate_positions_rejects_a_position_inside_the_window():
    with pytest.raises(ValueError, match="shorter than the 15s window"):
        tc.validate_positions([10], 15)
    assert tc.validate_positions([15, 600], 15) == [15, 600]


# --- CLI request planes (no network) ---------------------------------


def _matched_payload(position: int) -> dict:
    return {
        "status": "matched",
        "offset_seconds": 0.01,
        "confidence": 0.99,
        "margin": 0.4,
        "seek_ticks": position * 10_000_000,
    }


def _stub_route(monkeypatch, calls, *, cold_once: bool = False):
    """Record the route each position takes without touching the network."""

    def fake_stream(base_url, item_id, *, timeout):
        calls.append(("stream", base_url, item_id))
        return tc.StreamInfo(
            url="https://cdn.example/show/index.m3u8", type="m3u8", headers={}
        )

    def fake_capture(ffmpeg, stream, position, window, output):
        calls.append(("capture", position))
        _write_wav(output, window)
        return float(window)

    def fake_warm(base_url, item_id, candidate_id, *, timeout, log=None):
        calls.append(("warm",))
        return True

    state = {"posts": 0}

    def fake_post(
        base_url, item_id, candidate_id, position, window, clip, filename, *, timeout
    ):
        state["posts"] += 1
        calls.append(("align", position))
        if cold_once and state["posts"] == 1:
            return (
                400,
                {
                    "detail": {
                        "error": "invalid_request",
                        "message": tc.COLD_CACHE_MARKER,
                    }
                },
                0.01,
            )
        return 200, _matched_payload(position), 0.02

    monkeypatch.setattr(tc, "resolve_stream", fake_stream)
    monkeypatch.setattr(tc, "capture_clip", fake_capture)
    monkeypatch.setattr(tc, "warm_pair", fake_warm)
    monkeypatch.setattr(tc, "post_align", fake_post)
    monkeypatch.setattr(tc.shutil, "which", lambda name: f"/usr/bin/{name}")


_BASE_ARGS = [
    "kinotron:9709-gra-prestolv:s1e1",
    "uaserialspro:274-gra-prestoliv-s2011:s1e1",
    "--base-url",
    "http://127.0.0.1:8004",
    "--position",
    "120",
    "--position",
    "600",
]

_COLD_ONE_POSITION_ARGS = [
    "kinotron:9709-gra-prestolv:s1e1",
    "uaserialspro:274-gra-prestoliv-s2011:s1e1",
    "--base-url",
    "http://127.0.0.1:8004",
    "--position",
    "600",
]


def test_main_uses_the_cold_path_by_default(monkeypatch, capsys):
    calls: list[tuple] = []
    _stub_route(monkeypatch, calls)

    assert tc.main(_BASE_ARGS) == 0

    assert [call for call in calls if call[0] == "warm"] == []
    assert [call for call in calls if call[0] == "align"] == [
        ("align", 120.0),
        ("align", 600.0),
    ]
    assert "path      cold" in capsys.readouterr().out


def test_main_warm_flag_opts_into_the_title_search_warm_up(monkeypatch, capsys):
    calls: list[tuple] = []
    _stub_route(monkeypatch, calls)

    assert tc.main([*_BASE_ARGS, "--warm"]) == 0

    assert [call for call in calls if call[0] == "warm"] == [("warm",)]
    assert [call for call in calls if call[0] == "align"] == [
        ("align", 120.0),
        ("align", 600.0),
    ]
    assert "path      warm-up + cold retry" in capsys.readouterr().out


def test_main_does_not_retry_a_cold_cache_failure_by_default(monkeypatch, capsys):
    calls: list[tuple] = []
    _stub_route(monkeypatch, calls, cold_once=True)

    assert tc.main([*_COLD_ONE_POSITION_ARGS]) == 1

    assert [call for call in calls if call[0] == "warm"] == []
    assert [call for call in calls if call[0] == "align"] == [("align", 600.0)]
    assert "retry with --warm" in capsys.readouterr().out


def test_main_warm_flag_retries_a_cold_cache_failure_once(monkeypatch):
    calls: list[tuple] = []
    _stub_route(monkeypatch, calls, cold_once=True)

    assert tc.main([*_COLD_ONE_POSITION_ARGS, "--warm"]) == 0

    assert [call for call in calls if call[0] == "warm"] == [("warm",), ("warm",)]
    assert [call for call in calls if call[0] == "align"] == [
        ("align", 600.0),
        ("align", 600.0),
    ]


# --- CLI preconditions (no network) ----------------------------------


def test_main_rejects_a_non_episode_id_before_any_request():
    with pytest.raises(SystemExit) as exc:
        tc.main(["kinotron:movie:__movie__", "eneyida:ep:s1e1"])
    assert exc.value.code == 2


def test_main_rejects_a_short_position_before_any_request():
    with pytest.raises(SystemExit) as exc:
        tc.main(
            [
                "kinotron:show:s1e1",
                "eneyida:show:s1e1",
                "--position",
                "5",
            ]
        )
    assert exc.value.code == 2
