"""Ashdi player conversation (animeon's ashdi.vip player surface).

ONE owner for the ashdi dialect: the Playerjs ``file:'<m3u8>'`` page
scrape (the upstream Kotlin's ``processAshdiIframe``) and the
playlist-folder fallback introduced by the 2026-08-14 upstream drift
(episode rows stopped embedding ``videoUrl``/``fileUrl``; the direct
player endpoint still serves a serial page whose Playerjs playlist
carries every episode's m3u8).

Import direction: this helper -> stdlib + providers.base only — never
the adapter. The adapter lends its fetch paths (``get_html`` for the
player pages, ``get_json`` for the direct endpoint — canonical error
codes + ADR-0005 allowlist) at call time; a payload-shape change on
ashdi.vip touches exactly this file.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .base import ProviderError

#: Playerjs iframe's ``file:'<url>'`` m3u8.
_ASHDI_FILE_RE = re.compile(r"""file\s*:\s*['"]([^'"]+\.m3u8)['"]""")

#: The playlist page's ``file:'[...]'`` JSON value (translation folders
#: -> season folders -> episode entries).
_PLAYLIST_FILE_RE = re.compile(r"file:'((?:[^'\\]|\\.)*)'")


async def resolve_ashdi_iframe(
    iframe_url: str,
    http: httpx.AsyncClient,
    *,
    get_html: Any,
    headers: dict[str, str],
) -> str:
    """Fetch the Ashdi iframe page and extract the ``file:'<m3u8>'``
    value. The upstream Kotlin does the same. We append
    ``?player=animeon.club`` when the URL has no query string,
    otherwise the CDN returns the wrong page."""
    clean = iframe_url.rstrip("?")
    if "?" in clean:
        fetch_url = clean
    else:
        fetch_url = f"{clean}?player=animeon.club"
    page = await get_html(
        fetch_url,
        http,
        headers=headers,
    )
    match = _ASHDI_FILE_RE.search(page)
    if not match:
        raise ProviderError("parse_failed", "ashdi file: '...' missing")
    return match.group(1)


def resolve_playlist_page(
    page: str,
    *,
    translation_name: str,
    episode_num: int,
) -> str | None:
    """Parse an ashdi serial page's Playerjs playlist for one episode.

    The page's ``file:'[...]'`` value is a JSON playlist: translation
    folders -> season folders -> episode entries
    (``{"title": "Серія N", "file": "...m3u8"}``). Selects the wanted
    translation's folder (case-insensitive; first folder when no name
    matches, mirroring the upstream's pick-first behavior) and the
    ``Серія <episode_num>`` entry inside it. None when the page carries
    no parseable playlist or the wanted entry is absent.
    """
    match = _PLAYLIST_FILE_RE.search(page)
    if not match:
        return None
    try:
        raw = re.sub(r"\\'", "'", match.group(1))
        playlist = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(playlist, list):
        return None
    folders = [f for f in playlist if isinstance(f, dict)]
    trans_name = translation_name.strip().casefold()
    target = next(
        (
            f
            for f in folders
            if str(f.get("title") or "").strip().casefold() == trans_name
        ),
        None,
    )
    if target is None and folders:
        target = folders[0]
    if target is None:
        return None
    want = f"Серія {episode_num}"
    for season in target.get("folder") or []:
        if not isinstance(season, dict):
            continue
        for item in season.get("folder") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("title") or "").strip() == want:
                file_url = item.get("file")
                if isinstance(file_url, str) and file_url.endswith(".m3u8"):
                    return file_url
    return None

async def playlist_fallback(
    anime_id: int,
    episode_num: int,
    translation_name: str,
    player_name: str,
    http: httpx.AsyncClient,
    *,
    get_json: Any,
    get_html: Any,
    headers: dict[str, str],
    base_url: str,
    classify_translations: Any,
) -> str | None:
    """Resolve an episode through the direct player endpoint when the
    episode row carries no urls (upstream drift, 2026-08-14).

    ``/api/player/<playerId>/<translationId>`` answers
    ``{"videoUrl": "https://ashdi.vip/serial/<id>?..."}`` — the ashdi
    serial page whose Playerjs ``file:'[...]'`` playlist carries every
    episode's m3u8. ``classify_translations`` is the adapter's
    translation classifier (the ``gated`` verdict logic stays there).
    None when the direct endpoint, the page or the wanted entry is
    unavailable — the caller decides the typed verdict.
    """
    trans_name = translation_name.strip().casefold()
    doc = await get_json(
        f"{base_url}/api/player/{anime_id}/translations",
        http,
        headers=headers,
    )
    direct_url: str | None = None
    for trans in classify_translations(doc or {}):
        t = trans.get("translation") or {}
        if str(t.get("name") or "").strip().casefold() != trans_name:
            continue
        trans_id = t.get("id")
        for player in trans.get("player") or []:
            if str(player.get("name") or "").strip().casefold() != player_name.strip().casefold():
                continue
            player_id = player.get("id")
            if trans_id is None or player_id is None:
                continue
            direct = await get_json(
                f"{base_url}/api/player/{player_id}/{trans_id}",
                http,
                headers=headers,
            )
            if isinstance(direct, dict):
                value = direct.get("videoUrl")
                if isinstance(value, str) and value:
                    direct_url = value
    if direct_url is None:
        return None
    page = await get_html(
        direct_url,
        http,
        headers={"Referer": f"{base_url}/", **headers},
    )
    return resolve_playlist_page(
        page,
        translation_name=translation_name,
        episode_num=episode_num,
    )
