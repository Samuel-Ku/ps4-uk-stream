"""Profile warming + recommendation rows + LLM wiring (spec #309 T5).

The taste layer of the catalog state: the background content-profile
warm for the home groups (spec #252), the personalized rows built from
those profiles («Рекомендовано для тебе», «Схоже на X», genre rails),
and the LLM taste-profile refresh (spec #290). Profiles live in
``_stores._profiles``; this module owns everything that builds, scores
and refreshes them.

Depends on ``_stores`` (profiles, caches, playback/user-state entries)
and ``resolution`` (group-key + content resolution). Never imports the
snapshot or search modules — the snapshot module imports THIS module
(``_with_recommendation_rows`` / ``_warm_profiles``), so the package
dependency DAG stays acyclic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, cast

from .. import config as _config
from ..home import build_genre_rows
from ..http_client import get_client
from ..llm import active_profile, fetch_profile, set_active_profile
from ..models import ContentResponse, HomeResponse, HomeRow
from ..providers import PROVIDERS
from ..recommend import (
    ANCHOR_WEIGHTS,
    MAX_ANCHORS,
    ItemProfile,
    build_recommendation_rows,
    profile_from_content,
)
from ._stores import (
    _HOME_KEY,
    _profiles,
    content_cache,
    gated_cache,
    home_cache,
    playback_entries,
    recent_history_entries,
    recent_playback_entries,
    recent_search_queries,
    row_deep_cache,
)
from .resolution import _gate_cache_key, episode_group_key, resolve_group

log = logging.getLogger("cs_uk_api.catalog_state.warm")

#: Bounded concurrency for the background profile warm (spec #252: the
#: same bounded-concurrency pattern as the gate sweep).
_PROFILE_CONCURRENCY = 8


async def _warm_profiles(home: HomeResponse) -> None:
    """Background content-profile build for the home groups (spec #252).

    Bounded concurrency, piggybacking the shared content cache — only
    cold groups cost a fetch. On completion, if any NEW profile landed,
    the home cache is invalidated so the next read rebuilds the
    snapshot WITH the recommendation rows (they are computed at build
    time). A warm that adds nothing (steady state) never invalidates,
    so the rebuild→warm loop terminates. A failed profile is just a
    missing signal — never an error.
    """
    groups = sorted({it.group_key for row in home.rows for it in row.items})
    if not groups:
        return
    sem = asyncio.Semaphore(_PROFILE_CONCURRENCY)
    added = False

    async def _one(group_key: str) -> None:
        nonlocal added
        if group_key in _profiles:
            return
        per_provider = resolve_group(group_key)
        if per_provider is None:
            return
        provider_id, item = next(iter(per_provider.items()))
        cache_key = _gate_cache_key(item)
        if gated_cache.get(cache_key) is True:
            return
        cached = content_cache.get(cache_key)
        if cached is None:
            provider = PROVIDERS.get(provider_id)
            if provider is None:
                return
            async with sem:
                try:
                    _, _, external = item.id.partition(":")
                    cached = await provider.content(external, get_client())
                    content_cache.set(cache_key, cached)
                except Exception as e:  # noqa: BLE001 — a failed profile is just a missing signal
                    log.debug("profile warm failed group=%s err=%s", group_key, e)
                    return
        _profiles[group_key] = profile_from_content(cast(ContentResponse, cached))
        added = True

    tasks = [asyncio.create_task(_one(gk)) for gk in groups]
    await asyncio.wait(tasks, timeout=_config.SETTINGS.search_total_timeout_s)
    if added:
        home_cache.clear()
        # The next home rebuild changes the snapshot — drop the
        # snapshot-anchored deep pools with it (spec #305).
        row_deep_cache.clear()


def _recommendation_rows(rows: Sequence[HomeRow]) -> list[HomeRow]:
    """«Рекомендовано для тебе» + «Схоже на X» from the current taste
    signal (spec #252).

    Candidates are the snapshot's groups with a warm profile, excluding
    already-watched groups; anchors are the up-to-3 most recently
    watched items (recency-weighted); the similar row anchors on the
    single most recent in-progress title. Rows are omitted when there
    is no signal (no anchors, no queries) — empty rows don't ship.
    """
    home_items = [it for row in rows for it in row.items]
    if not home_items or not _profiles:
        return []
    home_by_key = {it.group_key: it for it in home_items}
    # Excluded set (#253 AC4): EVERY group behind a recorded playback
    # position is off the recommendation shelves — not just the few that
    # also anchor the taste profile.
    watched = {
        gk
        for item_id in playback_entries()
        if (gk := episode_group_key(item_id)) is not None
    }
    anchors: list[tuple[ItemProfile, float]] = []
    similar: tuple[ItemProfile, str] | None = None
    recency = 0
    for item_id in recent_playback_entries(MAX_ANCHORS):
        group_key = episode_group_key(item_id)
        if group_key is None:
            continue
        prof = _profiles.get(group_key)
        if prof is None:
            continue
        if recency < len(ANCHOR_WEIGHTS):
            anchors.append((prof, ANCHOR_WEIGHTS[recency]))
        recency += 1
        item = home_by_key.get(group_key)
        if similar is None and item is not None:
            similar = (prof, item.title)
    queries = recent_search_queries()
    # An active profile's idea rows are genre signal alone — they can
    # ship without any anchor/query taste signal (spec #290 user story
    # 5: the curated rows exist even when the viewer hasn't watched or
    # searched recently).
    profile = active_profile()
    if not anchors and not queries and not (profile and profile.row_ideas):
        return []
    return build_recommendation_rows(
        home_items=home_items,
        profiles=_profiles,
        watched=watched,
        anchors=anchors,
        similar_anchor=similar,
        queries=queries,
        profile=profile,
    )


def _with_recommendation_rows(rows: list[HomeRow]) -> list[HomeRow]:
    """Insert the recommendation rows after «Популярні зараз» (or the
    form-split recent rows when popular is absent), before the type
    rows (#252) — and append the genre rails (spec #263) at the end.

    Both personalized families need warm content profiles; with none
    they are simply omitted (no signal → no rows).
    """
    rec = _recommendation_rows(rows)
    out = list(rows)
    if rec:
        insert_at = 0
        for i, row in enumerate(out):
            # The last popular-or-recent table kind the home build
            # emitted; the personalized rows slot in right after it.
            # (The retired «Новинки» kind is gone from the scan —
            # spec #362 D.)
            if row.type in ("recent_movie", "recent_series", "popular"):
                insert_at = i + 1
        out[insert_at:insert_at] = rec
    genre = build_genre_rows(
        home_items=[it for row in out for it in row.items],
        profiles=_profiles,
    )
    if genre:
        out.extend(genre)
    return out


def _llm_history_signal() -> list[dict[str, object]]:
    """The up-to-10 most recent history items as taste signals.

    Each entry is {title, genres, year, form} for the LLM: the item id
    resolves to its merged group key through the series-group reverse
    lookup (episodes report ``provider:external:s1e1`` wire ids), and
    only groups with a warm content profile contribute — a cold group
    has no taste to signal. Titles come from the current home snapshot
    (profiles carry no title); a group outside the snapshot falls back
    to its group key.
    """
    home = cast("HomeResponse | None", home_cache.get(_HOME_KEY))
    title_by_key: dict[str, str] = {}
    if home is not None:
        title_by_key = {it.group_key: it.title for row in home.rows for it in row.items}
    out: list[dict[str, object]] = []
    for item_id in recent_history_entries(10):
        group_key = episode_group_key(item_id)
        if group_key is None:
            continue
        prof = _profiles.get(group_key)
        if prof is None:
            continue
        out.append(
            {
                "title": title_by_key.get(group_key, group_key),
                "genres": sorted(prof.genres),
                "year": prof.year,
                "form": prof.form,
            }
        )
    return out


async def refresh_profile(*, client: Any | None = None) -> bool:
    """One LLM taste-profile refresh (spec #290 user stories 10–12).

    Collects the signals (recent history via the series-group reverse
    lookup, recent queries, the profile-derived catalog genre
    vocabulary), calls the model, and installs the active profile on
    success. Returns True only when a profile was installed; on ANY
    failure (missing knobs, network error, invalid answer) the previous
    profile — or none — stays active and False is returned. Never
    raises, never blocks the home build: the LLM call happens here, not
    on the home path. ``client`` is the injectable seam (tests pass a
    fake; production uses the configured endpoint).
    """
    try:
        profile = await fetch_profile(
            history=_llm_history_signal(),
            queries=recent_search_queries(),
            genres=sorted({g for p in _profiles.values() for g in p.genres}),
            client=client,
        )
    except Exception as e:  # noqa: BLE001 — the layer degrades to inert
        log.warning("llm taste-profile refresh failed: %s", e)
        return False
    if profile is None:
        return False
    set_active_profile(profile)
    # The home rows are BUILT from the active profile — the new weights/
    # tags/ideas must surface on the next build, not after the 30-min
    # home TTL. Clearing only invalidates; the next request rebuilds.
    home_cache.clear()
    return True


def recommendation_stats() -> dict[str, int]:
    """Profile-store counts for the health surface (#253 AC5).

    Profiles warmed, search queries recorded, groups with a recorded
    playback position (the recommendation exclusion set) — all
    debuggable from the existing ``/api/health``, no new endpoint.
    """
    return {
        "profiles": len(_profiles),
        "queries": len(recent_search_queries()),
        "watched": len(playback_entries()),
    }
