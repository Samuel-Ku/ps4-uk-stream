"""Warm the Uakino browser session and verify uakino.best reachability.

The original ticket (#51) called for persisting cf_clearance cookies so
plain httpx could reuse them. Investigation showed that uakino.best's
Cloudflare managed challenge is silent (no cf_clearance is ever set) and
evaluated per request: only fetch() executed inside a loaded page passes,
while every API-level client gets 403. There is therefore no cookie to
persist; the "refresher" became a warm/verify probe that reports whether
uakino.best still serves playable content. It launches its own session
(which it closes with --close); the API provider boots its own session
lazily via get_session(), so this script does not share warm state with
the API process — treat it as a health check, not a pre-warmer.

Usage:
    python -m cs_uk_api.scripts.refresh_uakino
    python -m cs_uk_api.scripts.refresh_uakino --close
Exit code 0 = session verified, 1 = upstream unreachable.
"""

from __future__ import annotations

import argparse
import asyncio

from ..uakino_browser import SessionError, get_session

# Known-good content page + news_id used only as reachability probes
# (Дюна, 2021). The root path itself is not fetch()-able from inside
# the page (Cloudflare only serves it to navigations), so we probe real
# endpoints instead.
_PROBE_PATH = "/filmy/12567-dyuna.html"
_PROBE_NEWS_ID = "12567"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--close", action="store_true", help="close the warm session")
    args = parser.parse_args()

    session = get_session()
    try:
        page_status, page_text = await session.fetch(_PROBE_PATH, method="GET")
        if page_status != 200:
            print(f"FAIL content probe {_PROBE_PATH} answered {page_status}")
            return 1
        if "data-news_id" not in page_text:
            print("FAIL content probe has no playlists-ajax block")
            return 1
        print(f"OK content probe: {page_status} ({len(page_text)} chars)")

        pl_status, pl_text = await session.fetch(
            f"/engine/ajax/playlists.php?news_id={_PROBE_NEWS_ID}&xfield=playlist&time=1"
        )
        if pl_status != 200:
            print(f"FAIL playlists probe answered {pl_status}")
            return 1
        li_count = pl_text.count("<li")
        print(f"OK playlists probe: {pl_status}, li items: {li_count}")
        if li_count == 0:
            print("FAIL playlists probe returned no items")
            return 1
    except SessionError as e:
        print(f"FAIL {e}")
        return 1
    finally:
        if args.close:
            await session.close()
    print("PASS uakino session is warm")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
