"""Jellyfin facade router (spec #100, tickets #102 + #104).

Mounted on the existing FastAPI app at the Jellyfin paths — deliberately
NOT under ``/api/*``, so the native contract is untouched and a Jellyfin
client pointed at ``host:port`` finds a server without configuration.

Ticket #102: the handshake. Ticket #104: the catalog surface — views,
item listing, poster. Ticket #105: item detail + hierarchy. Ticket
#106: search mapping (``/Items?searchTerm=`` + ``/Search/Hints``) feeding
the shared merged search; PlaybackInfo. Ticket #107: the conditional
stream handler (``GET /Videos/{id}/stream``) with byte proxying, Range
support, and HLS segment rewriting. Ticket #108: sessions no-op
endpoints (``/Sessions/Playing|Progress|Stopped|Logout`` → 204), all
behind the same ``require_token`` gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
import uuid
from typing import Any, cast
from urllib.parse import unquote

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import row_kinds
from ..catalog import (
    card_for_group,
    extend_row_pool,
    genres_for_group,
    group_entries,
    group_sources,
    home_items_in_index_order,
    is_favorite,
    is_hard_unavailable,
    is_played,
    peek_group_content,
    playback_episode_pair,
    playback_positions,
    poster_url_for_group,
    profiles,
    recent_playback,
    refresh_profile,
    refresh_snapshot,
    resolve_item,
    search,
    set_favorite,
    set_played,
    snapshot,
    view_row_type_for_group,
    year_for_group,
)
from ..config import SETTINGS
from ..http_client import get_client
from ..models import (
    ContentResponse,
    Episode,
    HomeItem,
    HomeResponse,
    HomeRow,
    SearchGroup,
    SearchResult,
    Season,
)
from ..poster_proxy import fetch as fetch_poster_bytes
from ..recommend import similarity
from ..wire_identity import is_group_key
from . import dto, images
from .auth import require_token
from .delivery import register as register_delivery
from .hls_proxy import (
    _STREAM_MEMO as _STREAM_MEMO,  # noqa: PLC0414 (re-export: suite clears the memo via router)
)
from .models import (
    ActivityLogEntryQueryResult,
    AuthenticationResult,
    BaseItemDto,
    BaseItemDtoQueryResult,
    DeviceInfoDtoQueryResult,
    DisplayPreferencesDto,
    FolderStorageDto,
    ItemCounts,
    SearchHint,
    SearchHintResult,
    SystemInfoPublic,
    SystemStorageDto,
    UserDataResult,
    UserDto,
)
from .playback_info import register as register_playback_info
from .playback_reports import register as register_playback_reports

log = logging.getLogger("cs_uk_api.jellyfin")

router = APIRouter(tags=["jellyfin"])


def _compile_case_insensitive(path_format: str) -> re.Pattern[str]:
    """Compile a Jellyfin path template to a case-insensitive regex.

    Real Jellyfin routes case-insensitively (a client may send
    ``/Users/authenticatebyname`` where the API spells
    ``/Users/AuthenticateByName`` — Switchfin does exactly this).
    FastAPI/Starlette routing is case-sensitive, so the facade matches
    its own templates case-insensitively in a middleware and rewrites
    the scope path to the canonical spelling before routing.
    """
    pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path_format)
    return re.compile("^" + pattern + "$", re.IGNORECASE)


def normalize_jellyfin_path(path: str) -> str | None:
    """Canonical spelling of a Jellyfin facade path, or None.

    Matches ``path`` against every registered facade route template
    case-insensitively (so ``/Users/authenticatebyname`` lands on
    ``POST /Users/AuthenticateByName``) and returns the canonical form
    with path params preserved verbatim. Non-facade paths (the native
    ``/api/*`` surface) match nothing and yield None — the middleware
    then leaves the scope path untouched.
    """
    best_route = None
    best_match = None
    best_specificity = 1 << 30
    for route in router.routes:
        compiled = getattr(route, "_jf_ci_regex", None)
        if compiled is None:
            compiled = _compile_case_insensitive(route.path_format)  # type: ignore[attr-defined]
            route._jf_ci_regex = compiled  # type: ignore[attr-defined]
        m = compiled.fullmatch(path)
        if m is None:
            continue
        # Prefer the most specific template (fewest path params): the
        # parameterized ``/Users/{user_id}`` will also match a literal
        # ``/Users/AuthenticateByName`` — the fixed route must win, else
        # the middleware rewrites login into a GET-booked 405.
        specificity = len(m.groupdict())
        if specificity >= best_specificity:
            continue
        best_specificity = specificity
        best_route, best_match = route, m
        if best_specificity == 0:
            break
    if best_route is None or best_match is None:
        return None
    canonical: str = best_route.path_format  # type: ignore[attr-defined]
    for name, value in best_match.groupdict().items():
        canonical = canonical.replace("{" + name + "}", value)
    return canonical


#: What the server tells the client it is. The official Jellyfin apps
#: validate the server's product/version on connect and refuse anything
#: that doesn't look like a real Jellyfin ("unsupported version or
#: product"). Surface a genuine Jellyfin identity so any client accepts
#: the handshake; the facade itself is version-agnostic.
_PRODUCT = "Jellyfin Server"
_VERSION = "10.11.11"


def _user_name_for(user_id: str) -> str:
    """The display name backed by a remembered ``/Users/{id}`` check.

    The facade is stateless and cannot recall what the user typed at
    login; a stable label ("User") keeps every client's "signed in as"
    UI consistent without persisting anything (D8).
    """
    return "User"


def _server_id() -> str:
    """Stable per-process identity: deterministic hash of host:port.

    A restart keeps the same ServerId (clients pin it in their local
    database), while two different deployments differ.
    """
    return hashlib.sha256(f"{SETTINGS.host}:{SETTINGS.port}".encode()).hexdigest()[:16]


#: A view's ``Id`` is a deterministic uuid5 of the row kind (D5), so
#: the mapping is reversible and stable across restarts — a client's
#: cached library list keeps working. Table kinds resolve through
#: ``row_kinds.VIEW_TYPE_BY_ID``; non-table kinds (the ``genre:<slug>``
#: rails, the recipe-inserted personalized rows) resolve through the
#: same uuid5 formula (``_view_id_for``) and the snapshot-scan reverse
#: lookup.


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


#: View ids are deterministic 32-hex uuid5s (D5) — the shape that
#: distinguishes a view id from a ``g2:`` group key or an episode id on
#: the wire, so the snapshot-only resolution below can stay lazy.
_VIEW_ID_RE = re.compile(r"[0-9a-f]{32}")


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


def _row_dto(row: HomeRow, server_id: str) -> BaseItemDto:
    """One virtual library (D5): a ``CollectionFolder`` whose ``Id`` the
    client echoes back as ``parentId`` on ``/Items``.

    The CollectionType derives from the row-kind table (spec #362 B):
    a table kind carries its entry's mapping; rows outside the table
    (the recipe-inserted personalized rows, the ``genre:<slug>`` rails)
    stay CollectionType-less.
    """
    entry = row_kinds.ROW_KINDS.get(row.type)
    return dto.row_dto(
        row.title,
        server_id,
        view_id=_view_id_for(row.type),
        collection_type=entry.collection_type if entry is not None else None,
    )


def _item_dto(row: HomeRow, item: HomeItem, server_id: str) -> BaseItemDto:
    """One library card: Movie/Series item carrying the ``g2:`` id.

    Resolution wrapper (ticket #344): the card's Type is re-verified
    against the item's RESOLVED content when one is cached; the shaping
    itself (D9 poster tag, ParentId view, UserData) delegates to
    ``dto.item_dto``.

    Ticket #216: the section/URL heuristic is a cheap guess, the content
    page is the truth, and the grid must not promise a Type the detail
    page will contradict. ``peek_group_content`` is a cache-only read
    (never fetches), so the re-verification is free; an unresolved card
    keeps the snapshot's own form.
    """
    resolved = peek_group_content(item.group_key)
    form = resolved.form if resolved is not None else item.form
    return dto.item_dto(
        item,
        server_id,
        # The form is a required movie|series axis, both table kinds —
        # the Type derives from the row-kind table (spec #362 B).
        jf_type=row_kinds.ROW_KINDS[form].jf_type,
        parent_view_id=_view_id_for(row.type),
        user_data_value=_user_data(item.group_key),
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


def _group_cards(group_key: str) -> list[SearchResult]:
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


def _folder_storage(path: str) -> FolderStorageDto:
    """Storage row for one directory (spec #280): path, used bytes,
    free bytes from ``statvfs`` when the filesystem reports it.
    """
    used: int | None = None
    free: int | None = None
    try:
        if path and os.path.isdir(path):
            used = _dir_size(path)
            st = os.statvfs(path)
            free = st.f_bavail * st.f_frsize
    except OSError:  # a stat failure degrades to empty row
        pass
    return FolderStorageDto(Path=path or "", FreeSpace=free, UsedSpace=used)


def _dir_size(path: str) -> int:
    """Recursive byte total of ``path`` (cheap for one cache directory)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:  # a raced/unreadable file is skipped
                pass
    return total


def _storage_report() -> SystemStorageDto:
    """The dashboard storage table (spec #280).

    Real data only for the poster cache (``ImageCacheFolder`` — the one
    directory the facade actually writes) and the disk's free space;
    every other named folder is the honest empty row (no other on-disk
    state exists). ``Libraries`` is empty — the catalog is virtual.
    """
    poster_dir = SETTINGS.poster_cache_dir
    empty = FolderStorageDto(Path="")
    return SystemStorageDto(
        ProgramDataFolder=empty,
        WebFolder=empty,
        ImageCacheFolder=_folder_storage(poster_dir or ""),
        CacheFolder=empty,
        LogFolder=empty,
        InternalMetadataFolder=empty,
        TranscodingTempFolder=empty,
        Libraries=[],
    )


def _card_for_group(group_key: str) -> HomeItem | None:
    """The snapshot card for a ``g2:`` item, or None (ticket #224, #364).

    Delegates to the indexed seam.
    """
    return card_for_group(group_key)


def _card_dto(group_key: str, card: HomeItem, server_id: str) -> BaseItemDto:
    """Degraded detail built purely from the home snapshot card (#224).

    Resolution wrapper (ticket #344): the parent view and the canonical
    poster URL are scanned from the cached home here; the shaping
    delegates to ``dto.card_detail_dto``. The card-data counterpart of
    ``_content_dto``: a known card whose live ``content()`` resolution
    failed transiently (run8: animeon ``unreachable``/502 for the
    popular first card) still answers the detail with the card's own
    data — title, type, year, genres, poster tag, parent view — instead
    of a hard 404 that blanks the whole page mid-run. Deliberate 404s
    (cold cache, gated, blocked, unknown ids, season suffixes) never
    reach here — see ``is_hard_unavailable``.
    """
    return dto.card_detail_dto(
        group_key,
        card,
        server_id,
        parent_view_id=_view_id_for_item(group_key),
        poster_url=_poster_for(group_key),
    )


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


def _content_dto(group_key: str, content: ContentResponse, server_id: str) -> BaseItemDto:
    """Movie/Series detail built from a resolved ContentResponse.

    Resolution wrapper (ticket #344): the card fallbacks (year #220,
    genres #219), the owning view id and the canonical poster URL are
    scanned here; the shaping delegates to ``dto.content_detail_dto``.

    The poster URL follows the D9 coherence rule: the SAME home-card
    poster ``/Items/{id}/Images/Primary`` resolves wins; only a card
    without art falls back to ``content.poster`` — so the tag and the
    route always agree (a card with no art means no tag AND a 404
    image, never a dangling tag). Translations stay server-side — the
    wire carries no translation surface. The item id is the stateless
    ``g2:`` group key, so the client's bookmarks and the native route
    agree.
    """
    poster_url = _poster_for(group_key)
    if poster_url is None:
        poster_url = content.poster
    return dto.content_detail_dto(
        group_key,
        content,
        server_id,
        fallback_year=_year_for_group(group_key),
        fallback_genres=_genres_for_group(group_key),
        parent_view_id=_view_id_for_item(group_key),
        poster_url=poster_url,
        user_data_value=_user_data(group_key),
    )


def _episode_wire_id(provider_id: str, episode_id: str) -> str:
    """The existing provider-scoped episode id, unchanged (D2).

    Id grammar stays in the router (ticket #344): wire-identity
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


def _episode_dto(
    group_key: str,
    season: Season,
    episode: Episode,
    provider_id: str,
    server_id: str,
    series_name: str,
) -> BaseItemDto:
    """One Episode satellite (D2/D3): resolution wrapper — the
    provider-scoped wire id (id grammar stays in the router, ticket
    #344) and its UserData are resolved here; the shaping delegates to
    ``dto.episode_dto``."""
    wire_id = _episode_wire_id(provider_id, episode.id)
    return dto.episode_dto(
        group_key,
        season,
        episode,
        server_id,
        series_name,
        wire_id=wire_id,
        user_data_value=_user_data(wire_id),
    )


async def _user_views() -> BaseItemDtoQueryResult:
    """One virtual library per ``/api/home`` row, in home-row order (D5).

    Triggers the shared home build on a cold cache (the same cost as
    ``GET /api/home``), so a fresh client launch never sees an empty
    library list; afterwards it serves from the 30-min snapshot.
    """
    home = await refresh_snapshot()
    server_id = _server_id()
    dtos = [_row_dto(row, server_id) for row in home.rows]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


def _search_group_dto(group: SearchGroup, server_id: str) -> BaseItemDto:
    """One search-result card (ticket #106): pure pass-through to
    ``dto.search_card_dto`` (ticket #344)."""
    return dto.search_card_dto(group, server_id)


def _search_hint(group: SearchGroup) -> SearchHint:
    """One search-box hint (ticket #106): pure pass-through to
    ``dto.search_hint`` (ticket #344)."""
    return dto.search_hint(group)


async def _jf_search_groups(search_term: str) -> list[SearchGroup]:
    """The merged search groups behind a facade search, or [] (ticket #106).

    Feeds the interface's ``search`` (the exact fan-out the native
    ``/api/search`` route runs — same per-provider failure attribution,
    gated-item sweep, uakino skip, and 5-min cache), whose registration
    fold-in (US3) puts the searched groups into the shared group-key
    resolution map so a card opens in the #105 detail surface (search
    covers the whole catalog; most results are NOT in the 30-min home
    snapshot).

    Degrades to an empty result on total failure (every provider timed
    out — the native route's 502 ``search_timeout``) and on an empty
    term: the Jellyfin-tolerant answer, the same a stale view parent
    gets (D5).
    """
    term = search_term.strip()
    if not term:
        return []
    try:
        resp = await search(term)
    except HTTPException:
        return []
    return resp.groups


async def _jf_search(search_term: str) -> BaseItemDtoQueryResult:
    """Listing-shaped search result (ticket #106, D10): one card per
    merged group, ``g2:`` ids, Movie/Series types matching the #105
    detail surface."""
    groups = await _jf_search_groups(search_term)
    server_id = _server_id()
    dtos = [_search_group_dto(g, server_id) for g in groups]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


class AuthenticateByNameRequest(BaseModel):
    """Login body. Any username/password completes the handshake (D4)."""

    Username: str = ""
    Pw: str = ""


@router.get(
    "/System/Info/Public", response_model=SystemInfoPublic, response_model_exclude_none=True
)
async def system_info_public() -> SystemInfoPublic:
    """Server discovery: what a client hits first when adding the server.

    Unauthenticated by design (D4): the client needs this to render the
    login screen at all.
    """
    return SystemInfoPublic(
        LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
        ServerName=_PRODUCT,
        Version=_VERSION,
        ProductName=_PRODUCT,
        StartupWizardCompleted=True,
        Id=_server_id(),
    )


@router.get(
    "/System/Info",
    response_model=SystemInfoPublic,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def system_info(
    _token: str = Depends(require_token),
) -> SystemInfoPublic:
    """Full server info — authenticated in real Jellyfin.

    A client that has completed the handshake fetches this to confirm
    the server identity; the web UI reads ``ServerName``/``Version`` off
    it when reconnecting to a cached server. The first private facade
    route: proves the ``require_token`` gate on a real endpoint.
    """
    return SystemInfoPublic(
        LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
        ServerName=_PRODUCT,
        Version=_VERSION,
        ProductName=_PRODUCT,
        StartupWizardCompleted=True,
        Id=_server_id(),
    )


@router.get("/QuickConnect/Enabled", response_model=bool)
async def quickconnect_enabled() -> bool:
    """Advertise that QuickConnect login is off.

    Switchfin probes this before rendering the login screen and compares
    the raw body to ``"true"`` before showing the Quick Connect button.
    Real Jellyfin answers with a bare boolean, so the facade mirrors
    that: ``false`` keeps the client on the password path.
    """
    return False


@router.get(
    "/Branding/Configuration", response_model=dict[str, object], response_model_exclude_none=True
)
async def branding_configuration() -> dict[str, object]:
    """Empty branding block — the client falls back to defaults.

    Probed alongside ``/QuickConnect/Enabled`` during login-screen
    render. ``LoginDisclaimer`` must be a string, NOT null — Switchfin
    parses it into ``std::string`` via
    ``NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE_WITH_DEFAULT`` and a null value
    raises ``type_error.302`` on the console.
    """
    return {"LoginDisclaimer": ""}


@router.get(
    "/Plugins",
    response_model=list[object],
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def plugins() -> list[object]:
    """Plugin listing — always empty.

    Switchfin probes this on EVERY app start (``AppConfig::checkDanmuku``)
    to detect the Danmu plugin; an unimplemented route answered 404 and
    the client's HTTP layer logged "http status 404" on the console. An
    empty ``PluginList`` (bare JSON array) means "no plugins" and
    disables danmaku cleanly.
    """
    return []


@router.get(
    "/Users/{user_id}",
    response_model=UserDto,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def user_info(user_id: str) -> UserDto:
    """Persist a client's remembered session (Switchfin ``checkLogin``).

    On every start Switchfin calls ``GET /Users/{id}`` with the stored
    token to decide whether the previous login is still valid (config.cpp
    ``checkLogin``): a 200+parseable User keeps it in the main screen,
    anything else bounces it back to the login form. Since the facade is
    stateless and accepts any valid token, the remembered user is
    confirmed with a 200 echoing a stable UserDto — the client then skips
    re-authentication entirely.
    """
    return UserDto(
        Name=_user_name_for(user_id),
        ServerId=_server_id(),
        Id=user_id,
    )


@router.post(
    "/Users/AuthenticateByName",
    response_model=AuthenticationResult,
    response_model_exclude_none=True,
)
async def authenticate_by_name(
    body: AuthenticateByNameRequest,
) -> AuthenticationResult:
    """Accept-any-credentials login (D4): return the fixed token.

    The request username is echoed back as the user's name so the
    client's "signed in as X" UI shows what the user typed; nothing is
    stored (sessions are no-ops, D8).
    """
    token = SETTINGS.jellyfin_token
    server_id = _server_id()
    user = UserDto(
        Name=body.Username,
        ServerId=server_id,
        Id=uuid.uuid5(uuid.NAMESPACE_URL, f"cs-uk-api-user:{body.Username}").hex,
    )
    return AuthenticationResult(
        User=user,
        AccessToken=token,
        ServerId=server_id,
        SessionInfo=None,
    )


@router.get(
    "/UserViews",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def user_views_sdk(
    user_id: str | None = Query(default=None, alias="userId"),
) -> BaseItemDtoQueryResult:
    """SDK spelling of the views call (capture report, ticket #103).

    The official ``@jellyfin/sdk`` sends bare ``/UserViews?userId=…``
    rather than the server-style ``/Users/{id}/Views``; both spellings
    are served (capture verdict, ticket #103). The echoed ``User.Id``
    carries no server-side meaning — every client on this LAN is the
    same viewer.
    """
    return await _user_views()


@router.get(
    "/Users/{user_id}/Views",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def user_views_server(user_id: str) -> BaseItemDtoQueryResult:
    """Server-style spelling of the views call (spec D5)."""
    return await _user_views()


@router.get(
    "/Items",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def items_listing(
    parent_id: str | None = Query(default=None, alias="parentId"),
    user_id: str | None = Query(default=None, alias="userId"),
    search_term: str | None = Query(default=None, alias="searchTerm"),
    start_index: int = Query(default=0, alias="startIndex"),
    limit: int | None = Query(default=None),
    genre_ids: str | None = Query(default=None, alias="genreIds"),
    person_ids: str | None = Query(default=None, alias="PersonIds"),
    include_item_types: str | None = Query(default=None, alias="includeItemTypes"),
) -> BaseItemDtoQueryResult:
    """Library listing for one view, children of a series/season
    (ticket #105 hierarchy, D3), OR a merged-catalog search
    (ticket #106): when ``searchTerm`` is present, the listing is the
    shared ``/api/search`` merged groups as cards (``g2:`` ids, same
    Movie/Series shape as a view's cards).

    Two parent kinds are served by the same route:

      - ``parentId`` = a view's ``Id`` (echoed from ``/UserViews``) —
        the home-row cards, exactly the ticket #104 behaviour.
      - ``parentId`` = a series' ``g2:`` group key → the season list
        (``Type: Season``). ``parentId`` = a ``<group_key>:S<n>`` season
        id → the season's episodes (``Type: Episode``).

    A third mode (spec #272): when ``PersonIds`` is present the listing
    is the person's filmography — the home-snapshot groups whose warm
    content profile carries that person, filtered by
    ``IncludeItemTypes`` the way the client's person page asks
    (``Movie|Series``). No new scraping: the #252 profiles already hold
    people per title. An unknown person or a cold profile store is an
    empty result (never an error).

    Unknown or absent view → empty result (Jellyfin's tolerant answer
    for a stale parent, D5). Cold resolution cache or a movie parent →
    empty (a movie has no children, D3; episodes survive only under a
    resolved season).
    """
    if search_term:
        return await _jf_search(search_term)
    if person_ids:
        return _person_filmography(person_ids, include_item_types, limit, start_index)
    row_type, home = await _resolve_view_row_type(parent_id or "")
    if row_type is None:
        return await _hierarchy(parent_id)
    if home is None:
        home = await refresh_snapshot()
    server_id = _server_id()
    wanted_genres = _parse_genre_ids(genre_ids)
    for row in home.rows:
        if row.type == row_type:
            items = row.items
            if wanted_genres is not None:
                # Ticket #213: the genre shelf's tap round-trips as
                # ``genreIds=<id>`` — filter the view's cards to those
                # carrying at least one requested genre (genre ids ARE
                # the names).
                items = [it for it in items if wanted_genres & set(it.genres)]
            # Deep rows (spec #305): a page beyond the snapshot row
            # lazily EXTENDS the pool — provider browse pages 2..N
            # under the depth knob, merged with the home build's
            # round-robin + group-key dedupe — so infinite scroll keeps
            # serving NEW cards and ``TotalRecordCount`` grows honestly
            # (the client stops when a page comes back short). The
            # personalized and genre rails stay snapshot-bounded, and a
            # failing extension degrades to the snapshot slice.
            if wanted_genres is None and start_index >= len(items):
                extended = await extend_row_pool(row.type, items)
                if extended is not None:
                    items = extended
            dtos = [_item_dto(row, it, server_id) for it in items]
            total = len(dtos)
            end = None if limit is None else start_index + limit
            # Honest slicing (device-driving B11): the real client requests
            # ``startIndex``/``limit`` pages and stops when a page comes back
            # short. Ignoring the params made page 2 repeat page 1, so the
            # app's infinite scroll re-requested it forever.
            return BaseItemDtoQueryResult(
                Items=dtos[start_index:end],
                TotalRecordCount=total,
                StartIndex=start_index,
            )
    # A valid view id whose row is currently absent (e.g. «Популярні
    # зараз» when no provider carries it) is an empty library, not an
    # error — same tolerant answer as an unknown parent.
    return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)


def _person_filmography(
    person_ids: str,
    include_item_types: str | None,
    limit: int | None,
    start_index: int,
) -> BaseItemDtoQueryResult:
    """The person page's filmography (spec #272).

    ``PersonIds`` is comma-separated (the client's person page sends a
    single id); each is a provider-scoped person key whose FINAL path
    segment carries the display name (kinotron's
    ``/xfsearch/actors/<name>/`` → ``name``, uaserialspro's
    ``/person/<id>-<slug>/`` → ``<slug>`` — the same recovery the
    ``/Persons/{id}`` DTO uses). The name is matched
    case-insensitively against the profile store's people (the #252
    profiles hold ``people`` per title), and every home-snapshot group
    whose profile carries the person is returned as a card.

    ``IncludeItemTypes`` filters by form the same way the native catalog
    does (the client asks ``Movie|Series`` and renders two sections);
    a cold profile store or an unknown person yields the tolerant empty
    result — never an error.
    """
    wanted = {
        unquote(pid.rsplit(":", 1)[-1]).strip().lower()
        for pid in person_ids.split(",")
        if pid.strip()
    }
    if not wanted:
        return BaseItemDtoQueryResult()
    forms = {t.lower() for t in include_item_types.split("|")} if include_item_types else None
    profile_store = profiles()
    server_id = _server_id()
    dtos = []
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
        dtos.append(_item_dto(row, it, server_id))
    total = len(dtos)
    end = None if limit is None else start_index + limit
    return BaseItemDtoQueryResult(
        Items=dtos[start_index:end],
        TotalRecordCount=total,
        StartIndex=start_index,
    )


async def _resolve_playback_episode(
    item_id: str,
) -> tuple[BaseItemDto | None, BaseItemDto | None]:
    """Map a played episode id to (its DTO, the next episode's DTO).

    The DOMAIN walk — group reverse lookup, season/episode location,
    next sibling — lives behind the catalog seam
    (``playback_episode_pair``, #347); this wrapper is only the wire
    assembly, shaping the pairing's domain models through the same
    ``_episode_dto`` builder the season rail uses.
    Returns ``(None, None)`` for a non-episode id or an unresolvable
    group (cold cache / gated item).
    """
    pairing = await playback_episode_pair(item_id)
    if pairing is None:
        return None, None
    server_id = _server_id()
    episode_dto = _episode_dto(
        pairing.group_key,
        pairing.season,
        pairing.episode,
        pairing.provider_id,
        server_id,
        pairing.series_title,
    )
    next_dto = (
        _episode_dto(
            pairing.group_key,
            pairing.season,
            pairing.next_episode,
            pairing.provider_id,
            server_id,
            pairing.series_title,
        )
        if pairing.next_episode is not None
        else None
    )
    return episode_dto, next_dto


@router.get(
    "/Users/{user_id}/Items/Resume",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
@router.get(
    "/Items/Resume",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def items_resume(user_id: str = "u") -> BaseItemDtoQueryResult:
    """Continue-watching rail — items with a recorded position (#214).

    Both spellings serve the identical rail: the SDK's ``/Users/{id}/…``
    form and the bare ``/Items/Resume`` some clients (and careless
    users) type. ``user_id`` is not validated either way — the facade
    has a single fixed user (D4).

    Movies report their ``g2:`` key (PlaybackInfo on the movie card);
    episodes report the provider-scoped wire id, resolved through the
    group map. Both come back with ``PlaybackPositionTicks`` so the
    client renders the resume bar. The row returns the most recently
    updated items first, at most 20 (#249) — finished items were already
    dropped by the store. ``user_id`` is not validated — the facade has
    a single fixed user (D4).
    """
    dtos: list[BaseItemDto] = []
    for item_id, pos in recent_playback().items():
        position = pos.position_ticks
        runtime = pos.runtime_ticks
        if is_group_key(item_id):
            try:
                dto = await item_detail(item_id)
            except HTTPException:
                continue  # transiently unavailable item — skip, not fail
            dto.PlaybackPositionTicks = position
            if runtime is not None:
                dto.RunTimeTicks = runtime  # bar renders proportionally (#250)
            dtos.append(dto)
            continue
        episode_dto, _ = await _resolve_playback_episode(item_id)
        if episode_dto is not None:
            episode_dto.PlaybackPositionTicks = position
            if runtime is not None:
                episode_dto.RunTimeTicks = runtime
            dtos.append(episode_dto)
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


@router.get(
    "/Shows/NextUp",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def shows_next_up() -> BaseItemDtoQueryResult:
    """Next-up shelf: the next episode of each in-progress series (#214).

    One entry per series (the most-progressed episode's next sibling),
    in the same episode DTO shape the season rail hands out.
    """
    result: list[BaseItemDto] = []
    seen_series: set[str] = set()
    for item_id, pos in playback_positions().items():
        if is_group_key(item_id):
            continue  # a movie has no "next"
        runtime = pos.runtime_ticks
        _, next_episode = await _resolve_playback_episode(item_id)
        if next_episode is not None and next_episode.SeriesId not in seen_series:
            seen_series.add(next_episode.SeriesId or "")
            if runtime is not None:
                next_episode.RunTimeTicks = runtime  # same wire shape as Resume (#250)
            result.append(next_episode)
    return BaseItemDtoQueryResult(Items=result, TotalRecordCount=len(result))


@router.get(
    "/Shows/{series_id}/Seasons",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def shows_seasons(series_id: str) -> BaseItemDtoQueryResult:
    """Season rail of a series (Switchfin ``apiShowSeasons``).

    The client opens a series detail and issues this exact URL
    (``/Shows/{group}/Seasons?userId=…``); before this route existed it
    fell through to a 404 and the series' episodes were unreachable.
    Same D3 hierarchy lookup as ``parentId=<group key>`` on the items
    listing — the group key's season DTOs.
    """
    return await _hierarchy(series_id)


@router.get(
    "/Shows/{series_id}/Episodes",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def shows_episodes(
    series_id: str, season_id: str | None = Query(default=None, alias="seasonId")
) -> BaseItemDtoQueryResult:
    """Episode rail of one season (Switchfin ``apiShowEpisodes``).

    ``seasonId`` is the ``<group_key>:S<n>`` season id handed out by
    ``shows_seasons``; the same _hierarchy lookup as the items
    listing's season parent. ``series_id`` is not cross-checked (the
    id carries its own group key), it exists to match the client's URL.
    """
    return await _hierarchy(season_id)


@router.get(
    "/Users/{user_id}/Items/Latest",
    response_model=list[BaseItemDto],
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def items_latest(
    user_id: str,
    parent_id: str | None = Query(default=None, alias="parentId"),
) -> list[BaseItemDto]:
    """Latest-added shelf of a view — the view's home-row cards.

    Switchfin fetches this per library on the home screen
    (``…/Items/Latest?parentId=<view id>``) after listing the views,
    and parses the reply as a BARE JSON ARRAY of ``Episode`` structs
    (``getJSON<std::vector<jellyfin::Episode>>``), NOT the
    ``Result<T>`` envelope every other listing uses. Wrapping the items
    in ``{Items: […]}`` makes nlohmann fail the array read with
    ``type_error.302`` ("type must be array, but is object") — shown
    on the console as a "302" — so the wire shape here is a list, while
    the content stays the same row lookup as ``/Items``.
    """
    result = await items_listing(
        parent_id=parent_id,
        user_id=user_id,
        search_term=None,
        start_index=0,
        limit=None,
        genre_ids=None,
        person_ids=None,
        include_item_types=None,
    )
    return result.Items


@router.get(
    "/Users/{user_id}/Items",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def user_items_listing(
    user_id: str,
    parent_id: str | None = Query(default=None, alias="parentId"),
    search_term: str | None = Query(default=None, alias="searchTerm"),
    start_index: int = Query(default=0, alias="startIndex"),
    limit: int | None = Query(default=None),
    genre_ids: str | None = Query(default=None, alias="genreIds"),
    person_ids: str | None = Query(default=None, alias="PersonIds"),
    include_item_types: str | None = Query(default=None, alias="includeItemTypes"),
) -> BaseItemDtoQueryResult:
    """Server-style spelling of the library listing (Switchfin).

    Switchfin paths every library call under the user — ``/Users/{id}/
    Items?parentId=…`` (``apiUserLibrary``) rather than the bare
    ``/Items`` the SDK would use. Same wire dto, same row/hierarchy
    lookup as ``items_listing`` — including the ``searchTerm`` search
    surface (ticket #106: the SDK's ``getItems({searchTerm})`` spells
    exactly this URL) and the person-page ``PersonIds`` filmography
    (spec #272); registered after ``Resume``/``Latest`` so those
    literal segments win over this parameterized route.
    """
    return await items_listing(
        parent_id=parent_id,
        user_id=user_id,
        search_term=search_term,
        start_index=start_index,
        limit=limit,
        genre_ids=genre_ids,
        person_ids=person_ids,
        include_item_types=include_item_types,
    )


@router.get(
    "/Users/{user_id}/Items/{item_id}",
    response_model=BaseItemDto,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def user_item_detail(user_id: str, item_id: str) -> BaseItemDto:
    """Server-style item-detail spelling (Switchfin ``apiUserItem``).

    Same Movie/Series/Season DTO as the bare ``/Items/{item_id}``;
    Switchfin addresses detail pages as ``/Users/{id}/Items/{id}``.
    """
    return await item_detail(item_id)


@router.get(
    "/Persons/{person_id:path}",
    response_model=BaseItemDto,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def person_detail(person_id: str) -> BaseItemDto:
    """Person item — the People rail's tap target (ticket #221).

    The rail's ``Id`` values are provider-scoped person keys that carry
    the display name in the final path segment (kinotron's
    ``/xfsearch/actors/<name>/`` and uaserialspro's ``/person/<id>-<slug>/``
    links, decoded into the wire id; klontv ids are positional). The
    name is recovered from the id for the DTO — the facade has no
    per-person pages or portraits, so the DTO is identity-only. A
    malformed id degrades to the id itself as the name rather than
    erroring (a person tap must never break the detail page).
    """
    name = unquote(person_id.rsplit(":", 1)[-1])
    return BaseItemDto(
        Name=name,
        ServerId=_server_id(),
        Id=person_id,
        Type="Person",
    )


@router.get(
    "/Genres",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def genres(
    parent_id: str | None = Query(default=None, alias="parentId"),
    include_item_types: str | None = Query(default=None, alias="includeItemTypes"),
) -> BaseItemDtoQueryResult:
    """Genre filter shelf (Switchfin ``apiGenres``, ticket #213).

    Aggregates the ``genres`` metadata providers expose on their cards
    (e.g. ufdub's ``div.short-c``) into a per-view shelf: every unique
    genre across the view's cards, with ``ChildCount`` = how many cards
    carry it. The client opens the shelf per library with
    ``parentId=<view id>`` + ``includeItemTypes=<Movie|Series>``, so
    both are honored (an absent parent → empty shelf, the pre-#213
    behaviour real clients tolerated).

    Genre wire shape (Switchfin ``jellyfin::Genres``): ``{Id, Name,
    ImageTags, ChildCount}``. Id == Name, matching Jellyfin's own
    convention (genre ids are the names) so the id round-trips as the
    ``genreIds`` filter when the user taps a genre.
    """
    row_type, home = await _resolve_view_row_type(parent_id or "")
    if row_type is None:
        return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)
    if home is None:
        home = await refresh_snapshot()
    server_id = _server_id()
    want_type = _parse_include_types(include_item_types)
    counts: dict[str, int] = {}
    for row in home.rows:
        if row.type != row_type:
            continue
        for item in row.items:
            if want_type is not None and item.form not in want_type:
                continue
            for genre in item.genres:
                counts[genre] = counts.get(genre, 0) + 1
    dtos = [
        dto.genre_shelf_entry(genre, server_id, count) for genre, count in sorted(counts.items())
    ]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


@router.get(
    "/DisplayPreferences/usersettings",
    response_model=DisplayPreferencesDto,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def display_preferences() -> DisplayPreferencesDto:
    """Per-user sort/display prefs (Switchfin ``apiUserSetting``).

    No persistence exists yet; a neutral object tells the client "sort by
    name, ascending" — predictable and stable across navigations.
    """
    return DisplayPreferencesDto()


@router.get(
    "/Search/Hints",
    response_model=SearchHintResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def search_hints(
    search_term: str | None = Query(default=None, alias="searchTerm"),
) -> SearchHintResult:
    """Search-box hints (spec D10, ticket #106).

    The alternate search surface clients hit for the global search box
    (the web/desktop SDK's ``getSearchHints`` → ``/Search/Hints``) —
    same merged groups as ``/Items?searchTerm=``, in hint shape with the
    ``g2:`` ``ItemId`` the detail/image routes resolve. Missing or empty
    term → empty hints, never an error.
    """
    groups = await _jf_search_groups(search_term or "")
    hints = [_search_hint(g) for g in groups]
    return SearchHintResult(SearchHints=hints, TotalRecordCount=len(hints))


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


async def _hierarchy(parent_id: str | None) -> BaseItemDtoQueryResult:
    """Seasons of a series, or episodes of a season (D3, ticket #105).

    ``parent_id`` is a group key (series/movie → its seasons) or a
    season id (``<group_key>:S<n>`` → that season's episodes). A movie
    parent or an unresolved group key yields an empty result — the same
    tolerant answer a stale view gets (D5); a cold resolution cache
    means we cannot know the item, so there are no children to list.
    """
    if parent_id is None:
        return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)
    group_key, season_number = _split_season_suffix(parent_id)

    content = (await resolve_item(group_key)).content
    if content is None or content.seasons is None:
        return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)

    server_id = _server_id()
    provider_id = next(iter(content.id.split(":")), "")
    if season_number is None:
        # Series → its seasons; a movie resolves with seasons=None above.
        dtos = [dto.season_dto(group_key, s, server_id, content.title) for s in content.seasons]
    else:
        season = next((s for s in content.seasons if s.number == season_number), None)
        if season is None:
            return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)
        dtos = [
            _episode_dto(group_key, season, ep, provider_id, server_id, content.title)
            for ep in season.episodes
        ]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


@router.get(
    "/Items/Counts",
    response_model=ItemCounts,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def items_counts() -> ItemCounts:
    """Dashboard library-size row (spec #280): snapshot-derived counts.

    Movies/series are the forms the merged home snapshot actually
    knows; episodes come from the cached content pages (never a fetch);
    everything else the Jellyfin counts envelope names is structurally
    zero for this catalog and stays omitted.

    Registered BEFORE the ``/Items/{item_id}`` detail route: FastAPI
    matches in registration order, and ``Counts`` would otherwise be
    swallowed as an ``item_id`` and 404.
    """
    return _snapshot_counts()


@router.get(
    "/Items/{item_id}",
    response_model=BaseItemDto,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def item_detail(item_id: str) -> BaseItemDto:
    """Item detail (ticket #105, D2/D3): resolve a ``g2:`` key to its
    ContentResponse via the shared resolution map, and return a
    Movie/Series DTO.

    Unresolvable ids 404 with the same "item unavailable" verdict as a
    cold resolution cache (D2): ``g2:`` keys not in the cached home, and
    episode ids — served through the season listing, not reverse-
    resolvable on their own.
    """
    # Episode wire ids (``p1:s1e1``) are not reverse-resolvable: there is
    # no group key in them. They are served exclusively through the
    # season hierarchy, so /Items/{id} answers 404 for them.
    if not is_group_key(item_id):
        raise HTTPException(status_code=404, detail="item_unavailable")
    group_key, season_number = _split_season_suffix(item_id)
    content = (await resolve_item(group_key)).content
    if content is None:
        # Ticket #224: a card that IS in the home snapshot but whose
        # live resolution failed transiently (upstream blip/throttle)
        # answers with the card's own data instead of a hard 404 — the
        # same tolerant degradation _hierarchy gives an empty rail. The
        # deliberate 404s are untouched: a season suffix, an unknown or
        # cold-cache key, and a gated/blocked verdict (is_hard_unavailable)
        # all still 404 exactly as D2 prescribes.
        if season_number is None and not is_hard_unavailable(group_key):
            card = _card_for_group(group_key)
            if card is not None:
                return _card_dto(group_key, card, _server_id())
        raise HTTPException(status_code=404, detail="item_unavailable")

    if season_number is not None:
        if content.seasons is None:
            raise HTTPException(status_code=404, detail="item_unavailable")
        season = next((s for s in content.seasons if s.number == season_number), None)
        if season is None:
            raise HTTPException(status_code=404, detail="item_unavailable")
        return dto.season_dto(group_key, season, _server_id(), content.title)
    return _content_dto(group_key, content, _server_id())


@router.get(
    "/Items/{item_id}/Images/Primary",
)
async def item_primary_image(
    item_id: str, format: str | None = None, maxWidth: int | None = None
) -> Response:
    """Poster art (D9): the poster bytes, served directly with 200.

    The client's own ``format=Webp`` / ``maxWidth`` query (Switchfin
    always asks for ``Webp``) is honored: a non-WebP original is
    transcoded once (Pillow, resized to ``maxWidth`` when the original
    is larger) and cached per poster. Unknown item or poster-less item →
    404; Jellyfin clients render a placeholder instead of an image.

    Public on purpose: Jellyfin serves images without a token (media
    is addressable by URL), and client image loaders do not attach the
    ``X-Emby-Token`` header — requiring one here produces a wall of
    ``401 Unauthorized`` console errors.

    The bytes are fetched via the same ``fetch_poster_bytes`` cache the
    native ``/api/poster`` route uses, and returned inline — NOT as a
    302 to it. Switchfin's image loader does not chase a redirect; a
    redirect status is rendered as an error storm ("302") on the
    console while the home screen retries each card's art dozens of
    times (observed: ~72 attempts per poster).
    """
    return await _serve_item_image(item_id, format=format, maxWidth=maxWidth)


async def _serve_item_image(
    item_id: str, *, format: str | None = None, maxWidth: int | None = None
) -> Response:
    """The poster for ``item_id`` as an inline image response, or 404.

    Resolution (poster URL lookup + the shared poster-cache fetch) stays
    here; the WebP verdict/transcode delegates to the image module
    (ticket #343). ``fetch_poster_bytes`` resolves through THIS module at
    call time — the suite stubs it here.
    """
    poster_url = _poster_for(item_id)
    if poster_url is None and is_group_key(item_id):
        # Item not in the home snapshot (surfaced via Latest/search);
        # resolve from the content cache which holds the poster URL.
        content = (await resolve_item(item_id)).content
        poster_url = content.poster if content else None
    if poster_url is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    fetched = await fetch_poster_bytes(poster_url, get_client())
    if fetched is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    body, ctype = fetched
    if images.wants_webp(format) and not ctype.startswith("image/webp"):
        body = images.as_webp(poster_url, body, maxWidth)
        ctype = "image/webp"
    return Response(content=body, media_type=ctype)


@router.get(
    "/Users/{user_id}/Images/Primary",
)
async def user_primary_image(user_id: str, format: str | None = None) -> Response:
    """User avatar — no user concept on the facade.

    A transparent placeholder is served instead of a 404: Switchfin's
    server list always requests the avatar and logs "http status 404"
    on the console when it's missing. Public like every other image
    endpoint (token-less image loading, see ``item_primary_image``).
    """
    body, ctype = images.placeholder_avatar(format)
    return Response(content=body, media_type=ctype)


@router.get(
    "/Items/{item_id}/Images/Thumb",
)
@router.get(
    "/Items/{item_id}/Images/Logo",
)
@router.get(
    "/Items/{item_id}/Images/Backdrop",
)
@router.get(
    "/Items/{item_id}/Images/Backdrop/{index}",
)
async def item_auxiliary_image(item_id: str, index: int = 0) -> Response:
    """Backdrop/Logo/Thumb art — same poster bytes as ``Primary``.

    The catalog stores a single poster per item (no fanart, logos, or
    backdrops), so each variant serves that same image inline with 200,
    matching Switchfin's probe expectations (``apiThumbImage``/
    ``apiLogoImage``/``apiBackdropImage``). Public like all image
    endpoints; unknown/poster-less item → 404 (the client treats that
    as "no such art").
    """
    return await _serve_item_image(item_id)


@router.get(
    "/Items/{item_id:path}/Similar",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def item_similar(
    item_id: str,
    limit: int = Query(default=12),
) -> BaseItemDtoQueryResult:
    """Similar-shelf — profile-ranked "more like this" (spec #267 T1).

    The app fires this on every movie/series detail page; it used to
    answer a deliberately empty shelf, then (with genre metadata,
    #213) matched listing-card genres and stayed empty for providers
    that don't ship them. Now every home-snapshot group with a warm
    content profile is scored against the item's profile with the SAME
    weighted cosine the recommendation rows use (#252) and the shelf is
    ranked by that score — the item itself excluded, capped at
    ``limit`` (the client asks for 12). A cold profile store (or an
    item with no profile) falls back to the genre-matching shelf so
    the pre-#267 behaviour is preserved.

    The full ``BaseItemDtoQueryResult`` envelope is required: Switchfin
    parses every list response as ``Result<T>`` with
    ``NLOHMANN_JSON_FROM`` (no defaults), so a missing ``StartIndex``
    raised ``out_of_range.403`` on the console.
    """
    server_id = _server_id()
    item_profile = profiles().get(item_id)
    if item_profile is not None:
        scored: list[tuple[float, HomeRow, HomeItem]] = []
        scored_seen: set[str] = set()
        for entry in group_entries().values():
            it = cast(Any, entry).home_item
            if it is None or item_id == it.group_key or it.group_key in scored_seen:
                continue
            cand = profiles().get(it.group_key)
            if cand is None:
                continue
            score = similarity(item_profile, cand)
            if score <= 0:
                continue
            scored_seen.add(it.group_key)
            row = HomeRow(type=cast(Any, entry).row_type or "", title="", items=[it])
            scored.append((score, row, it))
        if scored:
            scored.sort(key=lambda t: t[0], reverse=True)
            dtos = [_item_dto(row, it, server_id) for _, row, it in scored[:limit]]
            return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))

    # Fallback: the genre-matching shelf (pre-#267 behaviour).
    wanted = set(_genres_for_group(item_id))
    if not wanted:
        return BaseItemDtoQueryResult()
    dtos = []
    seen: set[str] = set()
    for entry in group_entries().values():
        it = cast(Any, entry).home_item
        if it is None or item_id == it.group_key or it.group_key in seen:
            continue
        if not (set(it.genres) & wanted):
            continue
        seen.add(it.group_key)
        row = HomeRow(type=cast(Any, entry).row_type or "", title="", items=[it])
        dtos.append(_item_dto(row, it, server_id))
        if len(dtos) >= limit:
            break
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


@router.get(
    "/Users/{user_id}/Items/{item_id:path}/SpecialFeatures",
    response_model=list[object],
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def item_special_features(user_id: str, item_id: str) -> list[object]:
    return []


# ------------------------------------------------------------ user state (#257)


@router.post(
    "/Users/{user_id}/FavoriteItems/{item_id}",
    response_model=UserDataResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def favorite_add(user_id: str, item_id: str) -> UserDataResult:
    """Favorite an item (spec #257).

    The RESPONSE is the UserDataResult — Switchfin updates its heart
    button from the response's ``IsFavorite``, so a bare 204 would
    leave the button stuck. State is single-user (D4) and persists in
    the versioned user-state file.
    """
    set_favorite(item_id, True)
    return _user_data(item_id)  # type: ignore[return-value]


@router.delete(
    "/Users/{user_id}/FavoriteItems/{item_id}",
    response_model=UserDataResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def favorite_remove(user_id: str, item_id: str) -> UserDataResult:
    """Un-favorite an item (spec #257) — same response contract."""
    set_favorite(item_id, False)
    return _user_data(item_id)  # type: ignore[return-value]


@router.post(
    "/Users/{user_id}/PlayedItems/{item_id}",
    response_model=UserDataResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def played_add(user_id: str, item_id: str) -> UserDataResult:
    """Mark an item played (spec #257) — the context-menu affordance."""
    set_played(item_id, True)
    return _user_data(item_id)  # type: ignore[return-value]


@router.delete(
    "/Users/{user_id}/PlayedItems/{item_id}",
    response_model=UserDataResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def played_remove(user_id: str, item_id: str) -> UserDataResult:
    """Mark an item unplayed (spec #257) — same response contract."""
    set_played(item_id, False)
    return _user_data(item_id)  # type: ignore[return-value]


@router.get("/Sessions", dependencies=[Depends(require_token)])
async def sessions_list() -> list[dict[str, object]]:
    """Remote tab (spec #257): an empty session list — not a 404.

    No session store exists (D8) and no second clients exist (out of
    scope), so the honest answer is an empty list; Switchfin's Remote
    tab renders it without errors.
    """
    return []


@router.get(
    "/System/Info/Storage",
    response_model=SystemStorageDto,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def system_storage() -> SystemStorageDto:
    """Dashboard storage table (spec #280): real bytes, honest empties.

    The poster-cache directory's footprint (``ImageCacheFolder``) and
    the disk's free space are recomputed per call — cheap for one
    directory. Every other folder row is empty: the facade keeps no
    other on-disk state.
    """
    return _storage_report()


@router.get(
    "/Users",
    response_model=list[UserDto],
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def users_list() -> list[UserDto]:
    """Dashboard users row (spec #280): the single fixed facade user.

    The facade is single-user by design (D4); the list echoes that one
    user so the dashboard's users screen renders instead of erroring.
    """
    user_id = uuid.uuid5(uuid.NAMESPACE_URL, "cs-uk-api-user:default").hex
    return [UserDto(Name=_user_name_for(user_id), ServerId=_server_id(), Id=user_id)]


@router.get(
    "/ScheduledTasks",
    response_model=list[object],
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def scheduled_tasks() -> list[object]:
    """Dashboard scheduled-tasks row (spec #280): an empty task list.

    There is no task scheduler (out of scope), so the list is honestly
    empty in the standard ``TaskInfo[]`` shape — never a 404.
    """
    return []


async def _run_llm_profile_refresh() -> bool:
    """Injectable LLM-profile refresh seam (spec #290): the route
    answers through this so a test never hits a real model endpoint
    (same idiom as ``_schedule_restart``/``_exec_restart``)."""
    return await refresh_profile()


@router.post(
    "/ScheduledTasks/Running/llm-profile",
    dependencies=[Depends(require_token)],
)
async def run_llm_profile_refresh() -> Response:
    """On-demand LLM taste-profile refresh (spec #290 user story 11).

    Token-gated operator trigger in the dashboard's task idiom: a
    successful refresh answers 204 (the client's task-started
    contract); an inert/refused refresh answers 200 with a note —
    never an error (user story 8: a broken model answer leaves home
    unchanged). The real refresh runs through the injectable
    ``_run_llm_profile_refresh`` seam.
    """
    ok = await _run_llm_profile_refresh()
    if ok:
        return Response(status_code=204)
    return JSONResponse(
        {"Message": ("LLM taste profile not refreshed (not configured or the model call failed)")},
        status_code=200,
    )


@router.get(
    "/Devices",
    response_model=DeviceInfoDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def devices() -> DeviceInfoDtoQueryResult:
    """Dashboard devices row (spec #280): an empty device list.

    The facade is stateless (D8) — no device sessions are tracked — so
    the list is honestly empty in the standard query-result shape.
    """
    return DeviceInfoDtoQueryResult(Items=[], TotalRecordCount=0)


@router.get(
    "/System/ActivityLog/Entries",
    response_model=ActivityLogEntryQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def activity_log_entries() -> ActivityLogEntryQueryResult:
    """Dashboard activity-log row (spec #280): an honest empty log.

    No activity is tracked, so the log answers the standard
    ``ActivityLogEntryQueryResult`` with zero entries — never a 404.
    """
    return ActivityLogEntryQueryResult(Items=[], TotalRecordCount=0, StartIndex=0)


@router.post(
    "/Sessions/Capabilities/Full",
    dependencies=[Depends(require_token)],
)
async def sessions_capabilities_full() -> Response:
    """Client capability announcement (spec #280): accept, answer 204.

    Every Switchfin connect posts its playback capabilities here; a 404
    polluted startup logs. There is nothing to store — the facade is
    stateless (D8) — so the honest answer is 204.
    """
    return Response(status_code=204)


@router.get(
    "/LiveTv/Programs/Recommended",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def live_tv_recommended() -> BaseItemDtoQueryResult:
    """Live TV recommended programs (spec #280): an empty listing.

    There is no live source (out of scope, same as ``/LiveTv/Channels``
    #257), so the recommended-programs query answers the standard empty
    ``BaseItemDtoQueryResult`` — never a 404.
    """
    return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)


@router.get(
    "/LiveTv/Channels",
    response_model=BaseItemDtoQueryResult,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def live_tv_channels() -> BaseItemDtoQueryResult:
    """Live TV tab (spec #257): an empty channel listing — not a 404.

    There is no live source (out of scope), so the channel list is
    honestly empty; Switchfin's Live TV tab renders it without errors.
    """
    return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)


@router.post("/Sessions/Logout", dependencies=[Depends(require_token)])
async def sessions_logout() -> Response:
    """Session-end report (D8, capture verdict): accept, answer 204.

    The SDK's ``reportSessionEnded`` treats the logout call as the
    SignedOut signal and halts on anything but a 204, so the facade must
    answer exactly that. No token/session state is dropped — there is
    none to drop.
    """
    return Response(status_code=204)


#: Injectable re-exec seam (spec #280): the real restart swaps the
#: running process for a fresh one via ``os.execv``; tests replace this
#: with a recorder so a ``/System/Restart`` test never spawns a real
#: restart. Kept as a module-level callable so the route body stays
#: declarative.
def _exec_restart() -> None:
    """Replace the running process with the same command line (uvicorn
    relaunch) — the dashboard restart button's real action."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


#: Injectable schedule seam (spec #280): the route answers 204 first
#: and lets ``_schedule_restart`` defer the re-exec to a later loop
#: tick; tests replace it with a recorder so no real restart (or loop
#: pump) is needed.
def _schedule_restart() -> None:
    """Defer the re-exec one tick after the 204 response is sent."""
    asyncio.get_event_loop().call_later(0.1, _exec_restart)


@router.post("/System/Restart", dependencies=[Depends(require_token)])
async def system_restart() -> Response:
    """Dashboard restart button (spec #280): answer 204, then re-exec.

    The client expects a 204 response before the process disappears, so
    the re-exec is scheduled one event-loop tick AFTER the response is
    sent. ``_schedule_restart`` / ``_exec_restart`` are the injectable
    seams — LAN-only by design, an operator action (documented): the
    facade never exposes this over the open internet.
    """
    _schedule_restart()
    return Response(status_code=204)


@router.websocket("/socket")
async def websocket_socket(websocket: WebSocket) -> None:
    """Jellyfin WebSocket endpoint (``ws://host:port/socket``).

    Official Jellyfin clients open a WebSocket to ``/socket`` during
    connection validation — a strict handshake rejection surfaces as
    ``Invalid HTTP request received`` in uvicorn and the client reports
    "cannot connect". The token is enforced on the HTTP surface
    (``require_token``); the real Jellyfin accepts the socket eagerly and
    only pushes/ignores events, so the facade does the same: accept
    unconditionally (D4 accept-any posture) and keep the socket open.
    Incoming messages (presence/playback reports) are consumed and
    ignored; no response is written. The connection is torn down only
    when the client disconnects.
    """
    await websocket.accept()
    try:
        while True:
            # Consume client messages (presence/playback reports) and
            # ignore them — a real server would push session events here.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("websocket closed unexpectedly", exc_info=True)


# The Sessions/Playing* report conversation (parser + routes) lives in
# :mod:`playback_reports` — ONE owner for the client's playback-report
# surface (#108/#214/#248). Registered flat here so the wire surface
# and the facade's route table are unchanged.
# Delivery (stream/vtt/download/segment) first, then reports — the
# moved routes keep their relative declaration order. Table position
# (the tail) is inert: no two facade patterns match the same URL, and
# the full suite exercises every route through the real middleware.
register_delivery(router)
register_playback_info(router)
register_playback_reports(router)

__all__ = ["require_token", "router"]
