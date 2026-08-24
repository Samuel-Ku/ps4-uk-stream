"""Playlist walker covering 4 payload shapes (spec #361 batch 1)."""
from __future__ import annotations

import json
import re
from typing import Any

_DUB_PREFIX_RE = re.compile(r"^\{(.*?)\}")
_SEASON_TITLE_RE = re.compile(r"сезон|season", re.IGNORECASE)


def _strip_decorators(url: str) -> str:
    if url.startswith("{"):
        # {LABEL}https://...
        try:
            url = url.split("}", 1)[1]
        except IndexError:
            pass
    if "(subtitle:" in url:
        url = url.split("(subtitle:", 1)[0]
    return url.strip()


def _dub_prefix(file_value: str) -> str | None:
    m = _DUB_PREFIX_RE.match(file_value)
    return m.group(1).strip() if m else None


def _is_season_titled(entry: dict[str, Any] | None) -> bool:
    return bool(entry and _SEASON_TITLE_RE.search(str(entry.get("title", ""))))


def walk_playlist(
    payload: Any,
    season_idx: int,
    episode_idx: int,
    translation: str | None = None,
) -> str | None:
    """Resolve a stream URL from a decoded playlist payload.

    Covers 4 shapes:
      1. flat seasons (serialno/kinovezha/uaserialspro live)
      2. dub-wrapped top-level with title match + first-dub fallback (klontv)
      3. season-top dub-inside (eneyida #331)
      4. bare movie file URL (http...)

    ``season_idx`` and ``episode_idx`` are 1-based. Returns ``None``
    when out-of-range or payload malformed so caller can raise
    ``ProviderError("parse_failed", ...)``. Never silent-first fallback.
    """
    if season_idx < 1 or episode_idx < 1:
        return None

    # Bare string payload
    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            return None
        if s.startswith("http"):
            return _strip_decorators(s) or None
        if s.startswith("["):
            try:
                payload = json.loads(s)
            except json.JSONDecodeError:
                return None
        else:
            # unknown string shape
            return _strip_decorators(s) or None if s.startswith("{") else None

    if not isinstance(payload, list) or not payload:
        return None

    # Filter non-dict entries
    # For shapes detection, look at first dict
    first = next((x for x in payload if isinstance(x, dict)), None)
    if first is None:
        return None

    # Eneyida wrapper: single outer entry whose folder holds season-titled entries
    # e.g. payload = [{"folder": [{"title":"1 сезон",...}, ...]}]
    # Only unwrap when payload is single wrapper, not dub-wrapped multi-dub list.
    if len(payload) == 1 and not _is_season_titled(first):
        first_folder_tmp = first.get("folder") if isinstance(first.get("folder"), list) else []
        if first_folder_tmp and any(
            isinstance(x, dict) and _is_season_titled(x) for x in first_folder_tmp
        ):
            # unwrap one level – seasons live inside first's folder
            return walk_playlist(first_folder_tmp, season_idx, episode_idx, translation)

    # Detect season-top dub-inside: outer title looks like season
    if _is_season_titled(first):
        # Season-top: outer list = seasons
        if season_idx > len(payload):
            return None
        season = payload[season_idx - 1]
        if not isinstance(season, dict):
            return None
        dubs = season.get("folder") or []
        if not isinstance(dubs, list) or not dubs:
            return None
        # Determine if 3-level (season -> dubs -> episodes) or 2-level (season -> episodes)
        # If first dub has nested folder, it's 3-level
        first_dub = dubs[0] if dubs and isinstance(dubs[0], dict) else None
        is_three_level = isinstance(first_dub, dict) and "folder" in first_dub
        if is_three_level:
            # pick dub by translation
            selected_dub: dict[str, Any] | None = None
            if translation:
                for d in dubs:
                    if isinstance(d, dict) and str(d.get("title", "")).strip() == translation:
                        selected_dub = d
                        break
            if selected_dub is None:
                selected_dub = first_dub if isinstance(first_dub, dict) else None
            if selected_dub is None:
                return None
            episodes = selected_dub.get("folder") or []
            if not isinstance(episodes, list) or episode_idx > len(episodes) or episode_idx < 1:
                return None
            ep = episodes[episode_idx - 1]
            if not isinstance(ep, dict):
                return None
            return _strip_decorators(str(ep.get("file", ""))) or None
        else:
            # 2-level: season -> episodes directly (dubs are actually episodes)
            if episode_idx > len(dubs):
                return None
            ep = dubs[episode_idx - 1]
            if not isinstance(ep, dict):
                return None
            # translation handling for 2-level? If dubs are episodes, translation already handled?
            return _strip_decorators(str(ep.get("file", ""))) or None

    # Check for dub-wrapped vs flat by inspecting first's folder children
    first_folder = first.get("folder") or []
    if not isinstance(first_folder, list):
        first_folder = []
    # Determine if children have nested folder
    has_nested = False
    for child in first_folder:
        if isinstance(child, dict) and "folder" in child:
            has_nested = True
            break

    # Also check flat shape with {label} prefix alternative: flat episodes have file with http after prefix
    # If has_nested and outer not season-titled, it's dub-wrapped (klontv style)
    if has_nested:
        # dub-wrapped: outer = dubs, each dub's folder = seasons
        selected = None
        if translation:
            for dub in payload:
                if isinstance(dub, dict) and str(dub.get("title", "")).strip() == translation:
                    selected = dub
                    break
        if selected is None:
            selected = first if isinstance(first, dict) else None
        if selected is None:
            return None
        seasons = selected.get("folder") or []
        if not isinstance(seasons, list) or season_idx > len(seasons) or season_idx < 1:
            return None
        season = seasons[season_idx - 1]
        if not isinstance(season, dict):
            return None
        episodes = season.get("folder") or []
        if not isinstance(episodes, list) or episode_idx > len(episodes) or episode_idx < 1:
            return None
        ep = episodes[episode_idx - 1]
        if not isinstance(ep, dict):
            return None
        return _strip_decorators(str(ep.get("file", ""))) or None
    else:
        # flat seasons: outer = seasons, each season's folder = episodes
        if season_idx > len(payload):
            return None
        season = payload[season_idx - 1]
        if not isinstance(season, dict):
            return None
        episodes_raw = season.get("folder") or []
        if not isinstance(episodes_raw, list) or not episodes_raw:
            return None
        if translation:
            candidates = [
                ep for ep in episodes_raw
                if isinstance(ep, dict) and _dub_prefix(str(ep.get("file", ""))) == translation
            ]
            if candidates:
                # pick by episode_idx within candidates, else first candidate
                if episode_idx <= len(candidates):
                    ep = candidates[episode_idx - 1]
                else:
                    ep = candidates[0]
                if isinstance(ep, dict):
                    return _strip_decorators(str(ep.get("file", ""))) or None
        if episode_idx > len(episodes_raw):
            return None
        ep = episodes_raw[episode_idx - 1]
        if not isinstance(ep, dict):
            return None
        return _strip_decorators(str(ep.get("file", ""))) or None
