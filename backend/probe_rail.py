"""Per-provider episode-rail probe for #139 triage.

Walks the series episode-rail (same 4 hops as sweep_episode_rail) for a
single provider's series in the live home snapshot, and prints the
per-hop status plus the provider-level detail for any break:

    Seasons -> Episodes?seasonId -> POST PlaybackInfo -> GET stream

Usage:
    python probe_rail.py <provider_id> [base_url] [token]

Diagnostic-only, throwaway (not committed). The full sweep
(sweep_episode_rail.py) stays the authoritative acceptance gate; this
exists to triage WHICH hop breaks for a specific provider fast, and to
probe skipped providers (no series in the snapshot) via direct
content()/stream() calls.
"""
from __future__ import annotations

import asyncio
import json
import sys
from urllib.parse import quote

import httpx

from cs_uk_api.providers import PROVIDERS


def _walk_one(item: dict) -> None:
    """Print the rail for a single home item against the running server."""
    ...


async def probe_direct(provider_id: str) -> None:
    """Fallback for providers with no series in the snapshot: probe
    browse + content + stream directly to see whether the rail WOULD
    resolve if a series surfaced."""
    import httpx

    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import ProviderError

    provider = PROVIDERS.get(provider_id)
    if provider is None:
        print(f"no such provider: {provider_id}")
        return
    print(f"=== direct probe {provider_id} (browse sections) ===")
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as http:
        sections = list(getattr(provider, "sections", ())) or [("series", "series", "series")]
        seen: set[str] = set()
        checked = 0
        for section in sections:
            if checked >= 3:
                break
            sid = section.id if hasattr(section, "id") else section
            try:
                cards, has_next = await provider.browse(sid, 1, http)
            except Exception as e:  # noqa: BLE001
                print(f"  browse({sid}) ERR {type(e).__name__}: {e}")
                continue
            for card in cards:
                if checked >= 3:
                    break
                if card.id in seen:
                    continue
                seen.add(card.id)
                ext = card.id.split(":", 1)[1]
                try:
                    c = await provider.content(ext, http)
                except ProviderError as e:
                    print(f"  {ext[:36]:38} content ERR {e.code}: {e}")
                    checked += 1
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f"  {ext[:36]:38} content CRASH {type(e).__name__}: {e}")
                    checked += 1
                    continue
                nseas = len(c.seasons or [])
                neps = sum(len(s.episodes) for s in (c.seasons or []))
                ep = (c.seasons[0].episodes[0].id if c.seasons and c.seasons[0].episodes else None)
                sres = "N/A"
                if ep:
                    try:
                        st = await provider.stream(ep, None, http)
                        sres = f"OK {st.url[:45]}"
                    except ProviderError as e:
                        sres = f"ERR {e.code}"
                    except Exception as e:  # noqa: BLE001
                        sres = f"CRASH {type(e).__name__}"
                print(f"  {ext[:36]:38} seas={nseas} eps={neps} stream={sres}")
                checked += 1


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8003"
    token = "jellyfin-dev-token"
    provider_id = argv[1] if len(argv) > 1 else None
    if provider_id is None:
        print(__doc__)
        return 2
    if len(argv) > 2:
        base = argv[2]
    if len(argv) > 3:
        token = argv[3]

    # Import providers registry so PROVIDERS is populated.
    from cs_uk_api.providers import _registry  # noqa: F401

    # Determine the provider's series in the live snapshot.
    headers = {"X-Emby-Token": token}
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(f"{base}/api/home", headers=headers)
        if r.status_code != 200:
            print(f"cannot fetch home: {r.status_code}")
            return 1
        home = r.json()
        items = [
            item
            for row in home.get("rows", [])
            for item in row.get("items", [])
            if (item.get("providers") or []) and item["providers"][0] == provider_id
            and item.get("type") in {"series", "anime", "cartoon", "dorama"}
        ]

    if not items:
        print(f"{provider_id}: 0 series in home snapshot — direct probe:")
        asyncio.run(probe_direct(provider_id))
        return 0

    print(f"=== {provider_id}: {len(items)} series in snapshot ===")
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for item in items[:3]:
            gk = quote(item["group_key"], safe="")
            title = item.get("title", "?")
            print(f"\n-- {title} ({item['group_key']}) --")
            seasons = client.get(f"{base}/Shows/{gk}/Seasons", headers=headers)
            print(f"  Seasons        HTTP {seasons.status_code}")
            if seasons.status_code != 200:
                print(f"    body: {seasons.text[:200]}")
                continue
            data = seasons.json()
            seasons_items = data.get("Items", [])
            if not seasons_items:
                print("    -> 0 seasons (no_episodes at Seasons hop)")
                continue
            sid = seasons_items[0]["Id"]
            episodes = client.get(
                f"{base}/Shows/{gk}/Episodes?seasonId={quote(sid, safe='')}", headers=headers
            )
            print(f"  Episodes       HTTP {episodes.status_code} (season {sid})")
            if episodes.status_code != 200:
                print(f"    body: {episodes.text[:200]}")
                continue
            ep_items = episodes.json().get("Items", [])
            if not ep_items:
                print("    -> 0 episodes (no_episodes at Episodes hop)")
                continue
            ep_id = ep_items[0]["Id"]
            pb = client.post(f"{base}/Items/{quote(ep_id, safe='')}/PlaybackInfo", headers=headers)
            print(f"  PlaybackInfo   HTTP {pb.status_code} (ep {ep_id})")
            if pb.status_code != 200:
                print(f"    body: {pb.text[:200]}")
                continue
            st = client.get(
                f"{base}/Videos/{quote(ep_id, safe='')}/stream?static=true", headers=headers
            )
            print(f"  stream         HTTP {st.status_code}")

    # Health tracker state for this provider.
    from cs_uk_api.health import TRACKER

    status = TRACKER.status(provider_id)
    last_err = TRACKER.last_error_at(provider_id)
    print(f"\nhealth: status={status} last_error_at={last_err}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
