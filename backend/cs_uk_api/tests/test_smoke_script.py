"""Behavioural regression for ``deploy/smoke.sh`` dead-swarm failover.

Commit ``e6c223d`` moved the stream/bytes/seek checks inside
``candidate_play`` so a candidate whose swarm produces no bytes is an
item-level SKIP and the loop advances to the next candidate.  The live
regression that motivated it: ``g3:tt35969257`` resolved at PlaybackInfo
but its bounded read timed out (``000 0``), and the old script failed the
whole run after the candidate loop instead of trying
``g3:tt15239678``.

This test executes the real shell script with a fake ``curl`` on PATH, so
it pins the decision in the script itself: candidate 1 stalls, candidate
2 serves the 64 KiB and seek reads, and the gate exits 0 with exactly one
skip.  A second case pins the opposite outcome: with every candidate
stalling, each is skipped, the gate reports one failure, and it exits 1 —
the shape a dead lane or a misconfigured engine must produce.  A third
case covers the stale resolution map (#415): the first hit 404s at detail,
is skipped, and the next candidate still passes the gate.  A fourth case
pins the redirect check itself: a stream that answers a different 3xx is
skipped instead of read as a working redirect.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

SMOKE = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "smoke.sh"

FAKE_CURL = r'''#!/usr/bin/env python3
"""A tiny curl stand-in for the deploy smoke contract.

Only the flags smoke.sh uses are implemented.  Responses are chosen from
the request path; the dead candidate's engine read returns curl's
connection-timeout shape (http_code 000, zero bytes).  FAKE_STALL_ALL=1
gives every candidate's engine read that same shape; FAKE_DETAIL_404=1
makes the first search hit answer 404 at detail, the stale resolution-map
case (#415); FAKE_STREAM_STATUS overrides the status the facade's stream
endpoint answers with.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

args = sys.argv[1:]
method = "GET"
output = None
write_out = None
dump_header = None
range_header = None
url = None

i = 0
while i < len(args):
    arg = args[i]
    if arg in ("-X", "--request"):
        i += 1
        method = args[i]
    elif arg in ("-o", "--output"):
        i += 1
        output = args[i]
    elif arg in ("-w", "--write-out"):
        i += 1
        write_out = args[i]
    elif arg in ("-D", "--dump-header"):
        i += 1
        dump_header = args[i]
    elif arg in ("-d", "--data", "--data-raw"):
        i += 1
    elif arg in ("-H", "--header"):
        i += 1
    elif arg in ("-r", "--range"):
        i += 1
        range_header = args[i]
    elif arg in ("-m", "--max-time"):
        i += 1
    elif arg.startswith("-"):
        pass
    else:
        url = arg
    i += 1

if not url:
    sys.stderr.write(f"fake curl: no URL in {args!r}\n")
    raise SystemExit(99)

state = Path(os.environ["FAKE_CURL_STATE"])
with (state / "calls.log").open("a", encoding="utf-8") as log:
    log.write(f"{method} {url} range={range_header or '-'}\n")

# FAKE_STALL_ALL=1 models a dead lane: every candidate's swarm times out.
stall_all = os.environ.get("FAKE_STALL_ALL") == "1"

# FAKE_DETAIL_404=1 models a stale resolution map: the first hit is listed
# by search but has no resolution to open (#415).
detail_404 = os.environ.get("FAKE_DETAIL_404") == "1"

# FAKE_STREAM_STATUS overrides the facade's stream redirect status (302).
stream_status = os.environ.get("FAKE_STREAM_STATUS") or "302"

path = urlsplit(url).path
engine = os.environ["FAKE_ENGINE"]
status = "200"
size = 0
body = b""
redirect = ""
headers = ""

if path == "/Users/AuthenticateByName" and method == "POST":
    body = json.dumps({"AccessToken": "smoke-token", "ServerId": "smoke-server"}).encode()
elif path == "/UserViews":
    body = json.dumps({"Items": [{"Name": "Movies"}]}).encode()
elif path == "/api/sections":
    body = json.dumps(
        [{"provider": "yts", "sections": [{"id": "movies"}]}]
    ).encode()
elif path == "/api/browse":
    body = json.dumps({"results": [{"id": "card"}]}).encode()
elif path == "/api/search":
    body = json.dumps(
        {"groups": [{"group_key": "g3:dead"}, {"group_key": "g3:live"}]}
    ).encode()
elif path == "/Items/g3:dead" and detail_404:
    status = "404"  # the facade's shape for a key it cannot resolve
    body = json.dumps({"detail": "item_unavailable"}).encode()
elif path in ("/Items/g3:dead", "/Items/g3:live"):
    body = json.dumps({"Name": path.rsplit(":", 1)[1]}).encode()
elif path in ("/Items/g3:dead/PlaybackInfo", "/Items/g3:live/PlaybackInfo"):
    body = json.dumps(
        {"MediaSources": [{"Path": f"/Videos/{path.split('/')[2]}/stream"}]}
    ).encode()
elif path == "/Videos/g3:dead/stream":
    # The facade redirects the player; it never proxies media.
    status = stream_status
    redirect = f"{engine}/api/v1/torrent/dead/stream/0"
elif path == "/Videos/g3:live/stream":
    status = stream_status
    redirect = f"{engine}/api/v1/torrent/live/stream/0"
elif path == "/api/v1/torrent/dead/stream/0":
    status = "000"  # what curl reports when the dead swarm never answers
elif path == "/api/v1/torrent/live/stream/0":
    if stall_all:
        status = "000"  # every candidate is dead in the all-stall case
    elif range_header == "0-65535":
        status = "206"
        size = 65536
    elif range_header == "1048576-2097151":
        status = "206"
        size = 1048576
        headers = "Content-Range: bytes 1048576-2097151/3075917270\r\n"
    else:
        sys.stderr.write(f"fake curl: unexpected live range {range_header!r}\n")
        raise SystemExit(99)
else:
    sys.stderr.write(f"fake curl: unhandled {method} {url}\n")
    raise SystemExit(99)

if output and output != "/dev/null":
    Path(output).write_bytes(body)
if dump_header:
    Path(dump_header).write_text(headers, encoding="utf-8")

rendered = (write_out or "").replace("%{http_code}", status)
rendered = rendered.replace("%{size_download}", str(size))
rendered = rendered.replace("%{redirect_url}", redirect)
if rendered:
    sys.stdout.write(rendered)
'''


def _run_smoke(tmp_path, **env_overrides):
    """Run the real smoke.sh with the fake curl first on PATH.

    Returns the completed process and the fake's state directory (its
    ``calls.log`` records every request the script made).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(FAKE_CURL, encoding="utf-8")
    fake_curl.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()

    env = {key: value for key, value in os.environ.items() if not key.startswith("CS_UK_")}
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_CURL_STATE": str(state),
            "FAKE_ENGINE": "http://engine.test",
            "FAKE_STALL_ALL": "0",
            "FAKE_DETAIL_404": "0",
            "FAKE_STREAM_STATUS": "302",
            "CS_UK_API_URL": "http://api.test",
            "CS_UK_ENGINE_URL": "http://engine.test",
            "CS_UK_TIMEOUT": "5",
            "CS_UK_PLAY_TIMEOUT": "5",
            "CS_UK_TEST_TITLE": "Dune",
            **env_overrides,
        }
    )

    result = subprocess.run(
        ["bash", str(SMOKE)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, state


def test_dead_swarm_candidate_is_skipped_and_next_candidate_plays(tmp_path):
    result, state = _run_smoke(tmp_path)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert (
        "SKIP  g3:dead: bounded read failed (000 0; want 206/200 with 65536 bytes)"
        in output
    )
    assert "PASS  playable group g3:live -> /Videos/g3:live/stream" in output
    assert "=== 9 passed, 0 failed, 1 skipped ===" in output

    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert "GET http://engine.test/api/v1/torrent/dead/stream/0 range=0-65535" in calls
    assert "GET http://engine.test/api/v1/torrent/live/stream/0 range=0-65535" in calls
    assert (
        "GET http://engine.test/api/v1/torrent/live/stream/0 range=1048576-2097151"
        in calls
    )


def test_every_candidate_stalling_fails_the_gate(tmp_path):
    result, state = _run_smoke(tmp_path, FAKE_STALL_ALL="1")
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert (
        "SKIP  g3:dead: bounded read failed (000 0; want 206/200 with 65536 bytes)"
        in output
    )
    assert (
        "SKIP  g3:live: bounded read failed (000 0; want 206/200 with 65536 bytes)"
        in output
    )
    assert "FAIL  no candidate played 'Dune' — lane or engine is down" in output
    assert "=== 5 passed, 1 failed, 2 skipped ===" in output

    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert "GET http://engine.test/api/v1/torrent/dead/stream/0 range=0-65535" in calls
    assert "GET http://engine.test/api/v1/torrent/live/stream/0 range=0-65535" in calls
    # Both candidates died at the bounded read, so no seek was ever attempted.
    assert "range=1048576-2097151" not in calls


def test_detail_404_skips_to_the_next_candidate(tmp_path):
    """The #415 race: a search hit dropped from the resolution map 404s at
    detail.  The gate must skip that hit — not fail the run — and still
    pass on the next candidate."""
    result, state = _run_smoke(tmp_path, FAKE_DETAIL_404="1")
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "SKIP  g3:dead: detail -> 404 item_unavailable" in output
    assert "PASS  playable group g3:live -> /Videos/g3:live/stream" in output
    assert "=== 9 passed, 0 failed, 1 skipped ===" in output

    lines = (state / "calls.log").read_text(encoding="utf-8").splitlines()
    first_detail = lines.index("GET http://api.test/Items/g3:dead range=-")
    # The #415 mitigation: the initial search plus the pre-attempt re-issue
    # both ran before the detail read that 404s.
    searches_before = [ln for ln in lines[:first_detail] if "/api/search" in ln]
    assert len(searches_before) == 2


def test_non_302_stream_redirect_is_skipped(tmp_path):
    """The gate's PASS line claims a 302 to the engine; another 3xx (a bare
    RedirectResponse defaults to 307) must be an item-level skip, not read
    as the same contract."""
    result, state = _run_smoke(tmp_path, FAKE_STREAM_STATUS="307")
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "SKIP  g3:dead: stream -> 307 (want a 302 to the engine)" in output
    assert "SKIP  g3:live: stream -> 307 (want a 302 to the engine)" in output
    assert "FAIL  no candidate played 'Dune' — lane or engine is down" in output
    assert "=== 5 passed, 1 failed, 2 skipped ===" in output

    calls = (state / "calls.log").read_text(encoding="utf-8")
    # Both candidates stopped at the redirect check, before any engine read.
    assert "engine.test" not in calls
