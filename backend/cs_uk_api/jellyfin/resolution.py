"""Facade RESOLUTION half (deepening: the lookup side of the split).

``dto.py`` is the serialization half ("never scans state — the caller
resolves first"). THIS module is the other half: every cached-state
lookup, the view-id / episode-wire-id grammar, the include-types and
season-suffix parsers, and the person-page state walk. The router keeps
routing and DTO assembly; resolution.py never routes and never
serializes — it resolves state and wire ids into domain bits the caller
assembles.

Extracted verbatim from :mod:`router` (safe refactor, same
behavior-preservation bracket: the full suite pins the wire surface).
Functions keep their historical underscore names so route-level call
sites and the wire-shape tests are unchanged.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, cast
from urllib.parse import unquote

from .. import row_kinds
from ..catalog import (
    card_for_group,
    genres_for_group,
    group_entries,
    group_sources,
    home_items_in_index_order,
    is_favorite,
    is_played,
    peek_group_content,
    playback_positions,
    poster_url_for_group,
    profiles,
    refresh_snapshot,
    snapshot,
    view_row_type_for_group,
    year_for_group,
)
from ..models import HomeItem, HomeResponse, HomeRow
from ..wire_identity import is_group_key
from . import dto
from .models import ItemCounts, UserDataResult

#: View ids are deterministic 32-hex uuid5s (D5) — the shape that
#: distinguishes a view id from a ``g2:`` group key or an episode id on
#: the wire, so the snapshot-only resolution below can stay lazy.
_VIEW_ID_RE = re.compile(r"[0-9a-f]{32}")


def _view_id_for(row_type: str) -> str:
    """Deterministic view id for ANY home-row kind (spec #263).

    Every table kind's view id is exactly ``RowKind.view_id`` — the
    uuid5 of ``cs-uk-api-view:{kind}``; the ``genre:<slug>`` rails and
    the recipe-inserted personalized rows are NON-table kinds (spec
    #362 D1) that must resolve the same way. The uuid5 formula is
    deterministic and stable, so a client's cached library list keeps
    working across the retirement of «Новинки» — and the reverse
    ``_view_type_by_id`` recovers the row kind from any of these ids.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"cs-uk-api-view:{row_type}").hex


def _view_type_by_id(parent_id: str, home: HomeResponse | None = None) -> str | None:
    """Reverse of ``_view_id_for``: a view id → its home-row kind.

    Table kinds are pinned in ``row_kinds.VIEW_TYPE_BY_ID``; the
    snapshot-only rows (``genre:*``, recipe-inserted) resolve against a
    home snapshot — the caller's freshly-``load_home()``-ed one, else
    the cached snapshot. None for an unknown id or a cold cache (the
    caller then falls through to the tolerant empty answer).
    """
    t = row_kinds.VIEW_TYPE_BY_ID.get(parent_id)
    if t is not None:
        return t
    if home is None:
        home = snapshot()
    if home is None:
        return None
    for row in home.rows:
        if _view_id_for(row.type) == parent_id:
            return row.type
    return None


async def _resolve_view_row_type(
    parent_id: str,
) -> tuple[str | None, HomeResponse | None]:
    """(row kind, loaded home) for a view id, without a hierarchy build.

    Table kinds resolve from ``row_kinds.VIEW_TYPE_BY_ID``; the
    snapshot-only kinds (``genre:*``, recipe-inserted personalized rows)
    recover the row type from the cached home. When the cached home is
    mid-invalidation (the background profile-warm clears it), a 32-hex
    view id is re-resolved against a freshly ``load_home()``-ed snapshot
    so a view the client JUST listed never races into an empty grid.
    Non-view parents (a ``g2:`` group key, an episode id) return
    ``(None, None)`` — the caller's hierarchy path runs untouched, no
    home build.
    """
    row_type = _view_type_by_id(parent_id)
    if row_type is not None:
        return row_type, None
    if not _VIEW_ID_RE.fullmatch(parent_id):
        return None, None
    home = await refresh_snapshot()
    row_type = _view_type_by_id(parent_id, home)
    return row_type, home


def _parse_include_types(include_item_types: str | None) -> set[str] | None:
    """Parse ``includeItemTypes=Movie,Series`` into the home-row kinds.

    Reads ``row_kinds.KINDS_BY_JF_TYPE`` — the reverse index derived
    from the whole table (spec #362 B). None when the param is absent
    (no type filter); empty set when the param is present but names
    nothing we express (→ filter everything out, mirroring the client's
    expectation that an unexpressible type yields an empty shelf).
    """
    if include_item_types is None:
        return None
    kinds: set[str] = set()
    for t in include_item_types.split(","):
        kinds.update(row_kinds.KINDS_BY_JF_TYPE.get(t.strip(), frozenset()))
    return kinds


def _parse_genre_ids(genre_ids: str | None) -> set[str] | None:
    """Parse ``genreIds=a,b`` into a set (None when absent).

    Genre ids ARE the genre names (Jellyfin's convention), so the value
    round-trips directly as the shelf tap's filter (ticket #213).
    """
    if genre_ids is None:
        return None
    return {g for g in (x.strip() for x in genre_ids.split(",")) if g}


def _user_data(item_id: str | None) -> UserDataResult | None:
    """The UserDataResult for an item id (spec #257).

    Resolution wrapper (ticket #344): IsFavorite/Played come from the
    persisted user-state store and PlaybackPositionTicks from the
    playback store — read HERE; the wire shaping delegates to
    ``dto.user_data``.
    """
    if item_id is None:
        return None
    pos = playback_positions().get(item_id)
    return dto.user_data(
        item_id,
        favorite=is_favorite(item_id),
        played=is_played(item_id),
        position_ticks=pos.position_ticks if pos else None,
        runtime_ticks=pos.runtime_ticks if pos else None,
    )


def _home_items() -> list[tuple[HomeRow, HomeItem]]:
    """Every (row, item) pair in the cached home snapshot, or [].

    Deliberately does NOT trigger a home build — a read that would fan
    out to every provider belongs to the detail/list routes, not to
    cheap snapshot lookups (poster, similar shelf).
    """
    # Spec #364: index-backed, same order (row then item) as the
    # snapshot helper it replaces; callers needing the row use
    # group_entries() directly.
    items = home_items_in_index_order()
    # Reconstruct pairs via the index's row_type for callers that still
    # expect (row, item); row title is not used by the remaining callers.
    pairs: list[tuple[HomeRow, HomeItem]] = []
    for it in items:
        rt = view_row_type_for_group(it.group_key)
        row = HomeRow(type=rt or "", title="", items=[it])
        pairs.append((row, it))
    return pairs


def _group_cards(group_key: str) -> list[Any]:
    """Every card the resolution map holds for a ``g2:`` item, or [].

    Ticket #233: the #219/#220 fallbacks read the home snapshot, but a
    search-found group is usually NOT in the 30-min home snapshot — only
    in the shared group-key resolution map the interface's search
    populates (US3 fold-in). The detail DTO falls back across BOTH
    sources so a search-opened item renders the same metadata its own
    search card surfaced.
    """
    return group_sources(group_key)


def _genres_for_group(group_key: str) -> list[str]:
    """The card's genres for a ``g2:`` item, or [] (ticket #219, #364).

    Delegates to the indexed seam — home-snapshot card wins, then any
    card the resolution map holds (#233).
    """
    return genres_for_group(group_key)


def _year_for_group(group_key: str) -> int | None:
    """The card's year for a ``g2:`` item, or None (ticket #220, #364).

    Delegates to the indexed seam — home-snapshot card wins, then any
    card the resolution map holds (#233).
    """
    return year_for_group(group_key)


def _snapshot_counts() -> ItemCounts:
    """Library-size counts from the home snapshot (spec #280).

    Movies and series are the forms the merged home actually knows;
    episodes are counted from the cached content pages when available
    (a series whose seasons are in the content cache contributes its
    episode total), else zero — never a fetch. ``ItemCount`` is the
    movie+series sum, the number the dashboard headline shows.
    """
    movies = 0
    series = 0
    episodes = 0
    seen_groups: set[str] = set()
    home = snapshot()
    if home is not None:
        for row in home.rows:
            for it in row.items:
                if it.group_key in seen_groups:
                    continue
                seen_groups.add(it.group_key)
                if it.form == "movie":
                    movies += 1
                else:
                    series += 1
    # Episode total from the cached content pages (series only) — a
    # peek never fetches, so cold series simply contribute zero.
    for group_key in seen_groups:
        if _is_series_key(group_key):
            content = peek_group_content(group_key)
            if content is not None and content.seasons:
                episodes += sum(len(s.episodes) for s in content.seasons)
    return dto.item_counts(movies=movies, series=series, episodes=episodes)


def _is_series_key(group_key: str) -> bool:
    """True when the snapshot form for a group key is a series form.

    Cheap home-snapshot lookup mirroring ``_card_for_group``: a group
    whose card is a movie is a movie; everything else (series/anime/
    cartoon/dorama forms) is a series for counting purposes.
    """
    card = _card_for_group(group_key)
    if card is not None:
        return card.form != "movie"
    return not is_group_key(group_key) or True


def _card_for_group(group_key: str) -> HomeItem | None:
    """The snapshot card for a ``g2:`` item, or None (ticket #224, #364).

    Delegates to the indexed seam.
    """
    return card_for_group(group_key)


def _poster_for(item_id: str) -> str | None:
    """The canonical poster URL for a ``g2:`` item id, or None (spec #364).

    Delegates to the indexed seam.
    """
    return poster_url_for_group(item_id)


def _view_id_for_item(item_id: str) -> str | None:
    """The view id that surfaced a ``g2:`` item, from the index (spec #364)."""
    row_type = view_row_type_for_group(item_id)
    if row_type is None:
        return None
    return _view_id_for(row_type)


def _episode_wire_id(provider_id: str, episode_id: str) -> str:
    """The existing provider-scoped episode id, unchanged (D2).

    Id grammar stays on the resolution half (ticket #344): wire-identity
    consolidation is another wave's job. Providers are not uniform about
    whether ``episode.id`` already carries its ``{provider}:`` prefix
    (``uakino``/``kinotron`` embed it; most others emit a bare
    ``{external}:sXeY``). Reproduce exactly the id a native client hands
    ``/api/stream`` — parent provider prefix only when the episode id
    does not already start with it — so the PlaybackInfo/stream tickets
    can consume it unchanged.
    """
    if episode_id.startswith(f"{provider_id}:"):
        return episode_id
    return f"{provider_id}:{episode_id}"


def _split_season_suffix(parent_id: str) -> tuple[str, int | None]:
    """(group_key, season_number) for a season id, else (as-is, None).

    Season ids are ``<group_key>:S<n>`` (D2); the group key never
    carries an ``:S<n>`` tail, so ``rpartition`` cleanly separates the
    trailing season marker. A series/movie group key returns itself.
    """
    if not is_group_key(parent_id):
        return parent_id, None
    head, sep, tail = parent_id.rpartition(":")
    if sep and tail.startswith("S") and tail[1:].isdigit():
        return head, int(tail[1:])
    return parent_id, None


def person_filmography_pairs(
    person_ids: str,
    include_item_types: str | None,
) -> list[tuple[HomeRow, HomeItem]]:
    """The state walk of a person page (spec #272), as (row, item) pairs.

    ``PersonIds`` is comma-separated (the client's person page sends a
    single id); each is a provider-scoped person key whose FINAL path
    segment carries the display name (kinotron's
    ``/xfsearch/actors/<name>/`` → ``name``, uaserialspro's
    ``/person/<id>-<slug>/`` → ``<slug>`` — the same recovery the
    ``/Persons/{id}`` DTO uses). The name is matched case-insensitively
    against the profile store's people (the #252 profiles hold
    ``people`` per title), and every home-snapshot group whose profile
    carries the person is returned as a card. ``IncludeItemTypes``
    filters by form the same way the native catalog does; a cold
    profile store or an unknown person yields the tolerant empty walk —
    never an error. DTO assembly is the caller's job.
    """
    wanted = {
        unquote(pid.rsplit(":", 1)[-1]).strip().lower()
        for pid in person_ids.split(",")
        if pid.strip()
    }
    if not wanted:
        return []
    forms = {t.lower() for t in include_item_types.split("|")} if include_item_types else None
    profile_store = profiles()
    pairs: list[tuple[HomeRow, HomeItem]] = []
    seen: set[str] = set()
    for entry in group_entries().values():
        it = cast(Any, entry).home_item
        if it is None or it.group_key in seen:
            continue
        profile = profile_store.get(it.group_key)
        if profile is None:
            continue
        if not (wanted & profile.people):
            continue
        if forms is not None and it.form not in forms:
            continue
        seen.add(it.group_key)
        row = HomeRow(type=cast(Any, entry).row_type or "", title="", items=[it])
        pairs.append((row, it))
    return pairs