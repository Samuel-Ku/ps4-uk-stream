"""Capture-first request recording for the Jellyfin facade (ticket #103).

Capture-first is the spec's verb for pinning the endpoint surface: a
real Jellyfin client (here: the official ``@jellyfin/sdk``, the exact
network layer Jellyfin Web/desktop/Switchfin use) is driven against the
facade, and the request sequences it emits are frozen as fixtures.

Recording happens in the existing ``log_requests`` middleware — an
env-gated hook writes one JSONL record per facade request:

  - ``CS_UK_JF_CAPTURE_DIR`` (unset → disabled): directory to write
    ``capture.jsonl`` into.

Records carry the fields a contract test needs to *replay* the request
against the TestClient seam (method, path, query) plus the auth headers
a real client sends. Values that must not leak into the repo
(``X-Emby-Token``, ``MediaBrowser Token="..."``) are scrubbed to
``<scrubbed>``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from fastapi import Request, Response

log = logging.getLogger("cs_uk_api.jellyfin")

#: Header names captured verbatim; everything else is dropped.
_CAPTURED_HEADERS = ("x-emby-token", "authorization", "user-agent", "x-emby-authorization")

#: Facade namespaces. Segment-based so both spellings the SDK emits are
#: caught: ``/Users/{id}/Views`` (server-style) and ``/UserViews``,
#: ``/Items/{id}`` and bare ``/Items`` (SDK-style).
_FACADE_NAMESPACES = ("System", "Users", "UserViews", "Items", "Videos", "Sessions")

_SCRUB = "<scrubbed>"

def _scrub(_value: str) -> str:
    # ``X-Emby-Token`` values and ``MediaBrowser Token="…"`` inside
    # Authorization are the only secret-bearing material a capture carries;
    # the value is never needed, only its presence.
    return _SCRUB


def _is_facade(path: str) -> bool:
    parts = path.strip("/").split("/")
    return bool(parts and parts[0] in _FACADE_NAMESPACES)


def _capture_dir() -> str | None:
    raw = os.environ.get("CS_UK_JF_CAPTURE_DIR", "").strip()
    return raw or None


def capture_request(request: Request, response: Response) -> None:
    """Append one JSONL capture record for a facade request.

    Called from ``log_requests`` after the response is produced. No-op
    when the env gate is off or the request is not a facade path (the
    native ``/api/*`` surface is out of scope for capture).
    """
    path = request.url.path
    if not _is_facade(path):
        return
    capture_dir = _capture_dir()
    if capture_dir is None:
        return

    headers: dict[str, str] = {}
    for name in _CAPTURED_HEADERS:
        value = request.headers.get(name)
        if value is not None:
            headers[name] = _scrub(value)

    record: dict[str, Any] = {
        "ts": time.time(),
        "method": request.method,
        "path": path,
        "query": dict(request.query_params),
        "headers": headers,
        "status": response.status_code,
    }
    try:
        with open(os.path.join(capture_dir, "capture.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as e:  # pragma: no cover - capture is best-effort
        log.warning("jellyfin capture write failed: %s", e)