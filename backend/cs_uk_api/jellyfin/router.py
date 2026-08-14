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

import hashlib
import io
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..catalog_state import (
    episode_group_key,
    get_home,
    is_favorite,
    is_hard_unavailable,
    is_played,
    load_home,
    merged_search,
    peek_group_content,
    playback_entries,
    recent_playback_entries,
    record_playback,
    register_search_groups,
    resolve_group,
    resolve_group_content,
    set_favorite,
    set_played,
)
from ..config import SETTINGS
from ..health import TRACKER
from ..http_client import get_client
from ..models import (
    ContentResponse,
    Episode,
    HomeItem,
    HomeRow,
    SearchGroup,
    SearchResult,
    Season,
    StreamResponse,
)
from ..poster_proxy import fetch as fetch_poster_bytes
from ..providers import PROVIDERS
from ..providers.base import ProviderError
from .auth import require_token
from .models import (
    AuthenticationResult,
    BaseItemDto,
    BaseItemDtoQueryResult,
    DisplayPreferencesDto,
    MediaSourceInfo,
    PersonDto,
    PlaybackInfoResponse,
    SearchHint,
    SearchHintResult,
    SystemInfoPublic,
    UserDataResult,
    UserDto,
)

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


#: The home-row routing keys that can exist in a snapshot (v3 spec §3.1).
#: A view's ``Id`` is a deterministic uuid5 of one of these keys, so the
#: mapping is reversible (``_view_type_by_id``) and stable across
#: restarts — a client's cached library list keeps working.
_VIEW_TYPES = (
    "newest",
    "popular",
    # spec #252: the personalized rows are just more home-row kinds —
    # each becomes its own view, zero client changes.
    "recommended",
    "similar",
    "movie",
    "series",
    "anime",
    "cartoon",
    "dorama",
)

_VIEW_ID_BY_TYPE = {
    t: uuid.uuid5(uuid.NAMESPACE_URL, f"cs-uk-api-view:{t}").hex for t in _VIEW_TYPES
}
_VIEW_TYPE_BY_ID = {vid: t for t, vid in _VIEW_ID_BY_TYPE.items()}

#: Jellyfin ``CollectionType`` per home-row routing key (D5). The movie
#: row maps to the standard ``movies``; every other row is episodic-ish
#: and maps to ``tvshows``. No client we target branches deeper than
#: movie-vs-tvshows here.
_COLLECTION_TYPE_BY_ROW = {
    "movie": "movies",
    "series": "tvshows",
    "anime": "tvshows",
    "cartoon": "tvshows",
    "dorama": "tvshows",
    "newest": "tvshows",
    "popular": "tvshows",
    "recommended": "tvshows",
    "similar": "tvshows",
}

#: Home-row kind → Jellyfin item Type. Only Movie/Series are expressible
#: on the wire (AC: "correct Type (Movie/Series)"); style-tagged rows are
#: episodic content and become Series. Row kinds come from the Model B
#: section axes (contract #135) — the legacy ``type`` axis is gone.
_JF_TYPE_BY_ROW = {
    "movie": "Movie",
    "series": "Series",
    "anime": "Series",
    "cartoon": "Series",
    "dorama": "Series",
}

#: Reverse of ``_JF_TYPE_BY_ROW``: Jellyfin item Type → the home-row
#: kinds that map to it. Multiple rows collapse onto one wire Type
#: (series/anime/cartoon/dorama are all "Series"), so this is a
#: set-valued index — used to translate ``includeItemTypes`` back to
#: the row kinds the home snapshot is keyed by (ticket #213).
_HOME_KINDS_BY_JF_TYPE: dict[str, set[str]] = {}
for _kind, _jf_type in _JF_TYPE_BY_ROW.items():
    _HOME_KINDS_BY_JF_TYPE.setdefault(_jf_type, set()).add(_kind)


def _parse_include_types(include_item_types: str | None) -> set[str] | None:
    """Parse ``includeItemTypes=Movie,Series`` into the home-row kinds.

    None when the param is absent (no type filter); empty set when the
    param is present but names nothing we express (→ filter everything
    out, mirroring the client's expectation that an unexpressible type
    yields an empty shelf).
    """
    if include_item_types is None:
        return None
    kinds: set[str] = set()
    for t in include_item_types.split(","):
        kinds.update(_HOME_KINDS_BY_JF_TYPE.get(t.strip(), set()))
    return kinds


def _parse_genre_ids(genre_ids: str | None) -> set[str] | None:
    """Parse ``genreIds=a,b`` into a set (None when absent).

    Genre ids ARE the genre names (Jellyfin's convention), so the value
    round-trips directly as the shelf tap's filter (ticket #213).
    """
    if genre_ids is None:
        return None
    return {g for g in (x.strip() for x in genre_ids.split(",")) if g}


def _poster_tag(poster_url: str) -> str:
    """Opaque ``ImageTags.Primary`` value (D9).

    Deterministic in the poster URL, so a client-side image cache
    busts exactly when the upstream art changes and not otherwise.
    """
    return hashlib.sha256(poster_url.encode()).hexdigest()[:16]


def _user_data(item_id: str | None) -> UserDataResult | None:
    """The UserDataResult for an item id (spec #257).

    IsFavorite/Played come from the persisted user-state store;
    PlaybackPositionTicks from the playback store; PlayedPercentage
    derives from the position/runtime when known, else 100 when played
    and 0 otherwise. None when there is no id to look up (a view row,
    a season without a concrete item).
    """
    if item_id is None:
        return None
    result = UserDataResult(IsFavorite=is_favorite(item_id), Played=is_played(item_id))
    pos = playback_entries().get(item_id)
    if pos is not None:
        position, runtime = pos
        result.PlaybackPositionTicks = position
        if runtime and runtime > 0:
            result.PlayedPercentage = round(min(100.0, position / runtime * 100), 2)
        elif result.Played:
            result.PlayedPercentage = 100.0
    elif result.Played:
        result.PlayedPercentage = 100.0
    result.PlayCount = 1 if result.Played else 0
    return result


def _row_dto(row: HomeRow, server_id: str) -> BaseItemDto:
    """One virtual library (D5): a ``CollectionFolder`` whose ``Id`` the
    client echoes back as ``parentId`` on ``/Items``."""
    return BaseItemDto(
        Name=row.title,
        ServerId=server_id,
        Id=_VIEW_ID_BY_TYPE[row.type],
        Type="CollectionFolder",
        CollectionType=_COLLECTION_TYPE_BY_ROW.get(row.type),
    )


def _item_dto(row: HomeRow, item: HomeItem, server_id: str) -> BaseItemDto:
    """One library card: Movie/Series item carrying the ``g2:`` id.

    ``ImageTags.Primary`` is set only when the card carries a poster
    (D9). ``year`` is surfaced as ``ProductionYear`` (Jellyfin's field);
    ``ParentId`` is the view the card came from.

    Ticket #216: the card's Type is re-verified against the item's
    RESOLVED content when one is cached — the section/URL heuristic is a
    cheap guess, the content page is the truth, and the grid must not
    promise a Type the detail page will contradict. ``peek_group_content``
    is a cache-only read (never fetches), so the re-verification is free;
    an unresolved card keeps the snapshot's own form.
    """
    resolved = peek_group_content(item.group_key)
    form = resolved.form if resolved is not None else item.form
    dto = BaseItemDto(
        Name=item.title,
        ServerId=server_id,
        Id=item.group_key,
        Type=_JF_TYPE_BY_ROW.get(form, "Series"),
        ProductionYear=item.year,
        ParentId=_VIEW_ID_BY_TYPE[row.type],
        # Spec #257: hearts/played checkmarks/progress render from UserData.
        UserData=_user_data(item.group_key),
    )
    if item.poster is not None:
        dto.ImageTags = {"Primary": _poster_tag(item.poster)}
    return dto


def _home_items() -> list[tuple[HomeRow, HomeItem]]:
    """Every (row, item) pair in the cached home snapshot, or [].

    Deliberately does NOT trigger a home build — a read that would fan
    out to every provider belongs to the detail/list routes, not to
    cheap snapshot lookups (poster, similar shelf).
    """
    home = get_home()
    if home is None:
        return []
    return [(row, it) for row in home.rows for it in row.items]


def _group_cards(group_key: str) -> list[SearchResult]:
    """Every card the resolution map holds for a ``g2:`` item, or [].

    Ticket #233: the #219/#220 fallbacks read the home snapshot, but a
    search-found group is usually NOT in the 30-min home snapshot — only
    in the shared group-key resolution map ``register_search_groups``
    populated. The detail DTO falls back across BOTH sources so a
    search-opened item renders the same metadata its own search card
    surfaced.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return []
    return list(per_provider.values())


def _genres_for_group(group_key: str) -> list[str]:
    """The card's genres for a ``g2:`` item, or [] (ticket #219).

    The card parser (#213) harvests genre labels that the content page
    often does not repeat — ufdub's ``div.short-c`` lists them while the
    detail page carries only a description. The detail DTO falls back to
    this so the genre row renders where the data exists. First non-empty
    card wins: the home snapshot's card, then any card the group's
    resolution map holds (ticket #233).
    """
    for _row, it in _home_items():
        if it.group_key == group_key and it.genres:
            return list(it.genres)
    for card in _group_cards(group_key):
        if card.genres:
            return list(card.genres)
    return []


def _year_for_group(group_key: str) -> int | None:
    """The card's year for a ``g2:`` item, or None (ticket #220).

    Mirrors ``_genres_for_group``: a provider whose content page lacks
    the year meta block still gets the badge when the card carried a
    year. The content page wins when it has one — the card is the cheap
    guess. First year-ful card wins: the home snapshot's card, then any
    card the group's resolution map holds (ticket #233).
    """
    for _row, it in _home_items():
        if it.group_key == group_key and it.year is not None:
            return it.year
    for card in _group_cards(group_key):
        if card.year is not None:
            return card.year
    return None


def _card_for_group(group_key: str) -> HomeItem | None:
    """The snapshot card for a ``g2:`` item, or None (ticket #224).

    The degraded-detail lookup: when a card IS in the cached home but
    its live resolution failed transiently (upstream blip), the card
    itself still carries enough truth (title, year, genres, poster,
    view) to answer the detail. None when the item is not in the
    current home snapshot — a cold cache has no card, so the D2 404
    stands.
    """
    for _row, it in _home_items():
        if it.group_key == group_key:
            return it
    return None


def _card_dto(group_key: str, card: HomeItem, server_id: str) -> BaseItemDto:
    """Degraded detail built purely from the home snapshot card (#224).

    The card-data counterpart of ``_content_dto``: a known card whose
    live ``content()`` resolution failed transiently (run8: animeon
    ``unreachable``/502 for the popular first card) still answers the
    detail with the card's own data — title, type, year, genres,
    poster tag, parent view — instead of a hard 404 that blanks the
    whole page mid-run. Same lookups ``_content_dto`` falls back to
    (#219 genres, #220 year, D9 poster). Deliberate 404s (cold cache,
    gated, blocked, unknown ids, season suffixes) never reach here —
    see ``is_hard_unavailable``.
    """
    dto = BaseItemDto(
        Name=card.title,
        ServerId=server_id,
        Id=group_key,
        Type="Movie" if card.form == "movie" else "Series",
        ProductionYear=card.year,
        Genres=list(card.genres),
    )
    parent = _view_id_for_item(group_key)
    if parent is not None:
        dto.ParentId = parent
    poster = _poster_for(group_key)
    if poster is not None:
        dto.ImageTags = {"Primary": _poster_tag(poster)}
    return dto


def _poster_for(item_id: str) -> str | None:
    """The canonical poster URL for a ``g2:`` item id, or None.

    Resolution walks the cached home snapshot — the same lookup the
    native ``/api/content/{group_key}`` route uses — and takes the
    card's first-seen poster. A cold cache yields None → 404 ("item
    unavailable"), which Jellyfin clients tolerate per D2. Deliberately
    does NOT trigger a home build: an image request must not fan out to
    every provider.
    """
    home = get_home()
    if home is None:
        return None
    for row in home.rows:
        for it in row.items:
            if it.group_key == item_id:
                return it.poster
    return None


def _view_id_for_item(item_id: str) -> str | None:
    """The view id that surfaced a ``g2:`` item, from the cached home.

    Wraps the same home walk `_poster_for` uses so a detail page can
    tell the client which library the item belongs to (D5). None when
    the item is not in the current home snapshot.
    """
    home = get_home()
    if home is None:
        return None
    for row in home.rows:
        if any(it.group_key == item_id for it in row.items):
            return _VIEW_ID_BY_TYPE[row.type]
    return None


def _content_dto(group_key: str, content: ContentResponse, server_id: str) -> BaseItemDto:
    """Movie/Series detail built from a resolved ContentResponse.

    ``ImageTags.Primary`` iff the poster route would serve the item a
    poster (D9). The image tag is derived from the SAME home-card poster
    ``/Items/{id}/Images/Primary`` resolves — not ``content.poster`` —
    so the tag and the route always agree (a card with no art means no
    tag AND a 404 image, never a dangling tag). Translations stay
    server-side — the wire carries no translation surface. The item id
    is the stateless ``g2:`` group key, so the client's bookmarks and
    the native route agree.
    """
    dto = BaseItemDto(
        Name=content.title,
        ServerId=server_id,
        Id=group_key,
        Type="Movie" if content.form == "movie" else "Series",
        # Ticket #220: the content page carries the year when the
        # provider exposes one (ufdub's ``Рік:`` block); otherwise fall
        # back to the snapshot card's year so the badge renders where
        # either source has the data.
        ProductionYear=content.year
        if content.year is not None
        else _year_for_group(group_key),
        Overview=content.description,
        # Ticket #213: the detail page renders a genre row when present
        # (Switchfin ``media_movie``/``media_series`` show labelGenres
        # iff non-empty). Ticket #219: the content page often does NOT
        # repeat the card's genres (ufdub lists them on the card only) —
        # fall back to the snapshot card's genres so the row renders
        # where the data exists.
        Genres=list(content.genres or _genres_for_group(group_key)),
    )
    # Ticket #221: the People rail renders from BaseItemDto.People —
    # populated when the resolved provider's content page exposed cast
    # (kinotron/uaserialspro actor lists, klontv JSON-LD). Empty people
    # stays an empty list; Switchfin hides the rail then.
    dto.People = [
        PersonDto(Id=p.id, Name=p.name, Role=p.role) for p in content.people
    ]
    # Ticket #222: the rating badge renders from CommunityRating — set
    # when the provider exposed a real score (klontv's JSON-LD
    # aggregateRating); None stays omitted so the badge hides instead
    # of showing 0.
    dto.CommunityRating = content.rating
    parent = _view_id_for_item(group_key)
    if parent is not None:
        dto.ParentId = parent
    poster = _poster_for(group_key)
    if poster is None:
        poster = content.poster
    if poster is not None:
        dto.ImageTags = {"Primary": _poster_tag(poster)}
    # Spec #257: the detail screen's heart reads UserData.
    dto.UserData = _user_data(group_key)
    return dto


def _season_dto(group_key: str, season: Season, server_id: str, series_name: str) -> BaseItemDto:
    """One Season under a series (D2 ids ``<group_key>:S<n>``, D3).

    Carries the indexing fields Jellyfin clients breadcrumb on —
    ``IndexNumber`` = season number. Seasons get no ``ImageTags`` (D9).
    """
    return BaseItemDto(
        Name=f"Сезон {season.number}",
        ServerId=server_id,
        Id=f"{group_key}:S{season.number}",
        Type="Season",
        ParentId=group_key,
        SeriesId=group_key,
        SeriesName=series_name,
        IndexNumber=season.number,
    )


def _episode_wire_id(provider_id: str, episode_id: str) -> str:
    """The existing provider-scoped episode id, unchanged (D2).

    Providers are not uniform about whether ``episode.id`` already
    carries its ``{provider}:`` prefix (``uakino``/``kinotron`` embed
    it; most others emit a bare ``{external}:sXeY``). Reproduce exactly
    the id a native client hands ``/api/stream`` — parent provider
    prefix only when the episode id does not already start with it — so
    the PlaybackInfo/stream tickets can consume it unchanged.
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
    """One Episode satellite (D2: id keeps the provider-scoped episode
    suffix the PlaybackInfo/stream tickets consume; D3: ParentId = the
    owning season id).

    ``IndexNumber`` = number inside the season, ``ParentIndexNumber`` =
    the season number. No ``ImageTags`` (D9).
    """
    return BaseItemDto(
        Name=episode.title,
        ServerId=server_id,
        Id=_episode_wire_id(provider_id, episode.id),
        Type="Episode",
        ParentId=f"{group_key}:S{season.number}",
        SeriesId=group_key,
        SeriesName=series_name,
        IndexNumber=episode.number,
        ParentIndexNumber=season.number,
        # Ticket #223: the app asks for these fields explicitly
        # (``fields=...Overview``) — emit them when the provider's
        # episode data carries them (animeon's ``aired``); otherwise
        # omitted by ``response_model_exclude_none``.
        Overview=episode.description or None,
        PremiereDate=episode.premiere_date,
        # Spec #257: the played checkmark on episode rows reads UserData.
        UserData=_user_data(_episode_wire_id(provider_id, episode.id)),
    )


async def _user_views() -> BaseItemDtoQueryResult:
    """One virtual library per ``/api/home`` row, in home-row order (D5).

    Triggers the shared home build on a cold cache (the same cost as
    ``GET /api/home``), so a fresh client launch never sees an empty
    library list; afterwards it serves from the 30-min snapshot.
    """
    home = await load_home()
    server_id = _server_id()
    dtos = [_row_dto(row, server_id) for row in home.rows]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


def _search_group_dto(group: SearchGroup, server_id: str) -> BaseItemDto:
    """One search-result card (ticket #106): the merged group in the same
    listing shape as ``_item_dto`` (D5/D9) — ``g2:`` id, Movie/Series
    Type from the group's canonical type, ``ImageTags.Primary`` present
    *iff* the card has a poster.

    No ``ParentId``: a searched card is not tied to a home row — search
    covers the whole catalog, not a view.
    """
    dto = BaseItemDto(
        Name=group.title,
        ServerId=server_id,
        Id=group.group_key,
        Type=_JF_TYPE_BY_ROW.get(group.form, "Series"),
        ProductionYear=group.year,
    )
    if group.poster is not None:
        dto.ImageTags = {"Primary": _poster_tag(group.poster)}
    return dto


def _search_hint(group: SearchGroup) -> SearchHint:
    """One search-box hint (ticket #106): the same merged card in the
    ``SearchHint`` shape ``/Search/Hints`` serves."""
    hint = SearchHint(
        ItemId=group.group_key,
        Id=group.group_key,
        Name=group.title,
        Type=_JF_TYPE_BY_ROW.get(group.form, "Series"),
        ProductionYear=group.year,
    )
    if group.poster is not None:
        hint.ImageTags = {"Primary": _poster_tag(group.poster)}
    return hint


async def _jf_search_groups(search_term: str) -> list[SearchGroup]:
    """The merged search groups behind a facade search, or [] (ticket #106).

    Feeds the shared ``merged_search`` (the exact fan-out the native
    ``/api/search`` route runs — same per-provider failure attribution,
    gated-item sweep, uakino skip, and 5-min cache), then registers the
    groups into the shared group-key resolution map so a searched card
    opens in the #105 detail surface (search covers the whole catalog;
    most results are NOT in the 30-min home snapshot).

    Degrades to an empty result on total failure (every provider timed
    out — the native route's 502 ``search_timeout``) and on an empty
    term: the Jellyfin-tolerant answer, the same a stale view parent
    gets (D5).
    """
    term = search_term.strip()
    if not term:
        return []
    try:
        resp = await merged_search(term, provider="all", form=None, style_filter=None)
    except HTTPException:
        return []
    register_search_groups(resp.groups)
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


@router.get("/System/Info/Public", response_model=SystemInfoPublic, response_model_exclude_none=True)
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


@router.get("/System/Info", response_model=SystemInfoPublic, response_model_exclude_none=True, dependencies=[Depends(require_token)])
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


@router.get("/Branding/Configuration", response_model=dict[str, object], response_model_exclude_none=True)
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
    response_model=list[object], response_model_exclude_none=True,
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
    response_model=UserDto, response_model_exclude_none=True,
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


@router.post("/Users/AuthenticateByName", response_model=AuthenticationResult, response_model_exclude_none=True)
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
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
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
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def user_views_server(user_id: str) -> BaseItemDtoQueryResult:
    """Server-style spelling of the views call (spec D5)."""
    return await _user_views()


@router.get(
    "/Items",
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def items_listing(
    parent_id: str | None = Query(default=None, alias="parentId"),
    user_id: str | None = Query(default=None, alias="userId"),
    search_term: str | None = Query(default=None, alias="searchTerm"),
    start_index: int = Query(default=0, alias="startIndex"),
    limit: int | None = Query(default=None),
    genre_ids: str | None = Query(default=None, alias="genreIds"),
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

    Unknown or absent view → empty result (Jellyfin's tolerant answer
    for a stale parent, D5). Cold resolution cache or a movie parent →
    empty (a movie has no children, D3; episodes survive only under a
    resolved season).
    """
    if search_term:
        return await _jf_search(search_term)
    row_type = _VIEW_TYPE_BY_ID.get(parent_id or "")
    if row_type is None:
        return await _hierarchy(parent_id)
    home = await load_home()
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
                items = [
                    it for it in items if wanted_genres & set(it.genres)
                ]
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


async def _resolve_playback_episode(
    item_id: str,
) -> tuple[BaseItemDto | None, BaseItemDto | None]:
    """Map a played episode id to (its DTO, the next episode's DTO).

    Episode wire ids look like ``ufdub:dorama-408-...:s1e1`` — the
    ``provider:external`` prefix identifies the merged group (reverse
    group lookup, #214), whose season hierarchy gives the episode list.
    Returns ``(None, None)`` for a non-episode id or an unresolvable
    group (cold cache / gated item).
    """
    # The ``provider:external`` prefix before the episode tail
    # (``:s1e1`` / ``:e5`` / ``:eN:<blob>``) identifies the merged group
    # (shared helper, spec #252).
    group_key = episode_group_key(item_id)
    if group_key is None:
        return None, None
    seasons = (await _hierarchy(group_key)).Items
    for season in seasons:
        if season.Id is None:
            continue
        episodes = (await _hierarchy(season.Id)).Items
        for idx, episode in enumerate(episodes):
            if episode.Id == item_id:
                nxt = episodes[idx + 1] if idx + 1 < len(episodes) else None
                return episode, nxt
    return None, None


async def _record_playback_from(request: Request, *, flush: bool) -> None:
    """Best-effort store of the client's playback report (#214/#248).

    The @jellyfin/sdk posts PlaybackStartInfo/ProgressInfo/StopInfo
    bodies here; ``ItemId`` + ``PositionTicks`` are what a resume shelf
    needs, and ``RunTimeTicks`` rides along so a later tranche can mark
    finished items (spec #247). A malformed body is not an error — the
    report is advisory. ``flush=True`` (the Stopped path) persists the
    state file synchronously; heartbeat reports are debounced.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed report, keep the 204
        log.debug("playback report body unreadable, ignoring")
        return
    item_id = body.get("ItemId")
    position = body.get("PositionTicks")
    if isinstance(item_id, str) and isinstance(position, (int, float)):
        runtime = body.get("RunTimeTicks")
        runtime_ticks = int(runtime) if isinstance(runtime, (int, float)) else None
        record_playback(item_id, int(position), runtime_ticks=runtime_ticks, flush=flush)


@router.get(
    "/Users/{user_id}/Items/Resume",
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def items_resume(user_id: str) -> BaseItemDtoQueryResult:
    """Continue-watching rail — items with a recorded position (#214).

    Movies report their ``g2:`` key (PlaybackInfo on the movie card);
    episodes report the provider-scoped wire id, resolved through the
    group map. Both come back with ``PlaybackPositionTicks`` so the
    client renders the resume bar. The row returns the most recently
    updated items first, at most 20 (#249) — finished items were already
    dropped by the store. ``user_id`` is not validated — the facade has
    a single fixed user (D4).
    """
    dtos: list[BaseItemDto] = []
    for item_id, (position, runtime) in recent_playback_entries().items():
        if item_id.startswith("g2:"):
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
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def shows_next_up() -> BaseItemDtoQueryResult:
    """Next-up shelf: the next episode of each in-progress series (#214).

    One entry per series (the most-progressed episode's next sibling),
    in the same episode DTO shape the season rail hands out.
    """
    result: list[BaseItemDto] = []
    seen_series: set[str] = set()
    for item_id, (_position, runtime) in playback_entries().items():
        if item_id.startswith("g2:"):
            continue  # a movie has no "next"
        _, next_episode = await _resolve_playback_episode(item_id)
        if next_episode is not None and next_episode.SeriesId not in seen_series:
            seen_series.add(next_episode.SeriesId or "")
            if runtime is not None:
                next_episode.RunTimeTicks = runtime  # same wire shape as Resume (#250)
            result.append(next_episode)
    return BaseItemDtoQueryResult(Items=result, TotalRecordCount=len(result))


@router.get(
    "/Shows/{series_id}/Seasons",
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
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
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
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
    response_model=list[BaseItemDto], response_model_exclude_none=True,
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
    )
    return result.Items


@router.get(
    "/Users/{user_id}/Items",
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def user_items_listing(
    user_id: str,
    parent_id: str | None = Query(default=None, alias="parentId"),
    search_term: str | None = Query(default=None, alias="searchTerm"),
    start_index: int = Query(default=0, alias="startIndex"),
    limit: int | None = Query(default=None),
    genre_ids: str | None = Query(default=None, alias="genreIds"),
) -> BaseItemDtoQueryResult:
    """Server-style spelling of the library listing (Switchfin).

    Switchfin paths every library call under the user — ``/Users/{id}/
    Items?parentId=…`` (``apiUserLibrary``) rather than the bare
    ``/Items`` the SDK would use. Same wire dto, same row/hierarchy
    lookup as ``items_listing`` — including the ``searchTerm`` search
    surface (ticket #106: the SDK's ``getItems({searchTerm})`` spells
    exactly this URL); registered after ``Resume``/``Latest`` so those
    literal segments win over this parameterized route.
    """
    return await items_listing(
        parent_id=parent_id,
        user_id=user_id,
        search_term=search_term,
        start_index=start_index,
        limit=limit,
        genre_ids=genre_ids,
    )


@router.get(
    "/Users/{user_id}/Items/{item_id}",
    response_model=BaseItemDto, response_model_exclude_none=True,
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
    response_model=BaseItemDto, response_model_exclude_none=True,
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
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
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
    row_type = _VIEW_TYPE_BY_ID.get(parent_id or "")
    if row_type is None:
        return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)
    home = await load_home()
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
        BaseItemDto(
            Name=genre,
            ServerId=server_id,
            Id=genre,
            ChildCount=count,
        )
        for genre, count in sorted(counts.items())
    ]
    return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))


@router.get(
    "/DisplayPreferences/usersettings",
    response_model=DisplayPreferencesDto, response_model_exclude_none=True,
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
    response_model=SearchHintResult, response_model_exclude_none=True,
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
    if not parent_id.startswith("g2:"):
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

    content = await resolve_group_content(group_key)
    if content is None or content.seasons is None:
        return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)

    server_id = _server_id()
    provider_id = next(iter(content.id.split(":")), "")
    if season_number is None:
        # Series → its seasons; a movie resolves with seasons=None above.
        dtos = [_season_dto(group_key, s, server_id, content.title) for s in content.seasons]
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
    "/Items/{item_id}",
    response_model=BaseItemDto, response_model_exclude_none=True,
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
    if not item_id.startswith("g2:"):
        raise HTTPException(status_code=404, detail="item_unavailable")
    group_key, season_number = _split_season_suffix(item_id)
    content = await resolve_group_content(group_key)
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
        return _season_dto(group_key, season, _server_id(), content.title)
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


#: Transcode memo: ``(poster_url, max_width) → WebP bytes``. The client
#: retries a bad poster dozens of times per second (observed 533), so
#: the conversion must cost one Pillow pass per poster, not per request.
_WEBP_MEMO: dict[tuple[str, int | None], bytes] = {}
_WEBP_MEMO_MAX = 256


def _as_webp(poster_url: str, body: bytes, max_width: int | None) -> bytes:
    """``body`` as WebP bytes (Pillow), resized to ``max_width`` when
    larger; the original back on any decode error (a transcode failure
    must not turn a served poster into a 404)."""
    key = (poster_url, max_width)
    hit = _WEBP_MEMO.get(key)
    if hit is not None:
        return hit
    try:
        from PIL import Image, ImageOps

        logo: Image.Image = Image.open(io.BytesIO(body))
        if max_width and logo.width > max_width:
            logo = ImageOps.contain(logo, (max_width, max_width))
        out = io.BytesIO()
        logo.convert("RGB").save(out, format="WEBP", quality=82)
        hit = out.getvalue()
    except Exception:  # noqa: BLE001
        hit = body
    if len(_WEBP_MEMO) >= _WEBP_MEMO_MAX:
        _WEBP_MEMO.clear()
    _WEBP_MEMO[key] = hit
    return hit


async def _serve_item_image(
    item_id: str, *, format: str | None = None, maxWidth: int | None = None
) -> Response:
    """The poster for ``item_id`` as an inline image response, or 404."""
    poster_url = _poster_for(item_id)
    if poster_url is None and item_id.startswith("g2:"):
        # Item not in the home snapshot (surfaced via Latest/search);
        # resolve from the content cache which holds the poster URL.
        content = await resolve_group_content(item_id)
        poster_url = content.poster if content else None
    if poster_url is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    fetched = await fetch_poster_bytes(poster_url, get_client())
    if fetched is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    body, ctype = fetched
    wants_webp = format is not None and format.lower().lstrip("/") in ("webp", "webp,kwebp")
    if wants_webp and not ctype.startswith("image/webp"):
        body = _as_webp(poster_url, body, maxWidth)
        ctype = "image/webp"
    return Response(content=body, media_type=ctype)


#: Placeholder avatar bytes per format — the facade has no user concept,
#: but Switchfin's server list ALWAYS requests each saved user's avatar
#: (``apiUserImage``) and its HTTP layer logs any 4xx as a console error
#: ("http status 404"). A transparent placeholder answers 200 while still
#: rendering as "no avatar": the client's own default glyph shows through
#: the transparency.
_AVATAR_MEMO: dict[str, bytes] = {}


def _placeholder_avatar(format: str | None) -> tuple[bytes, str]:
    """A transparent placeholder image in the requested format.

    Switchfin's PS4 build requests ``format=Webp`` and decodes the body
    as WebP whenever the URL contains ``Webp`` (``Image::doRequest``), so
    the placeholder must be real WebP bytes — a PNG answer to a WebP URL
    silently fails to decode and renders nothing.
    """
    wants_webp = format is not None and format.lower().lstrip("/") in ("webp", "webp,kwebp")
    key = "webp" if wants_webp else "png"
    hit = _AVATAR_MEMO.get(key)
    if hit is not None:
        return hit, "image/webp" if wants_webp else "image/png"
    from PIL import Image

    img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    out = io.BytesIO()
    img.save(out, format="WEBP" if wants_webp else "PNG")
    hit = out.getvalue()
    _AVATAR_MEMO[key] = hit
    return hit, "image/webp" if wants_webp else "image/png"


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
    body, ctype = _placeholder_avatar(format)
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


async def _resolve_stream(item_id: str) -> StreamResponse | None:
    """The upstream ``StreamResponse`` behind a playable item id, or None.

    Resolves the two playable id families (D2/D3) to their provider, then
    runs ``provider.stream()`` exactly as the native ``/api/stream/{id}``
    route does — same bare external ids, ``translation=None`` (default
    voice), same shared ``httpx`` client:

      - a movie's ``g2:`` group key → the group's first-seen provider
        (the same provider the detail page shows first), whose BARE
        external id is what stream() consumes. Playability is decided on
        the content's FORM — ``content.form == "movie"`` — the same
        verdict detail renders as ``Type="Movie"``, NOT the card's style
        literal (``SearchResult.type`` can say ``"anime"`` for an anime
        FILM; conflating style with form would 404 a film the client just
        opened as a Movie). The shared ``resolve_group_content`` also
        carries the blocklist verdict, so a blocked title never gets a
        stream.
      - an episode wire id (``p1:s1e1``-style) → the provider is the id
        prefix; the episode suffix is handed to ``stream()`` exactly, no
        group-key resolution (episodes are not reverse-lookupable, D2).

    Unplayable ids (a series/season item — a show is not a playable
    thing, the client plays episodes, D3 — a season suffix, a cold group
    key, a blocked title, an unknown provider prefix) yield None. Rather
    than a middle-man tuple hop, the resolution and the stream call live
    in the same module: this IS the seam the routes cross, so a refusal
    degrades to None → 404, the facade's standing "never 5xx" posture
    (D2), and the provider+health recording stays colocated with it.
    """
    if item_id.startswith("g2:"):
        # Series/season keys and cold groups: not playable on their own.
        group_key, season_number = _split_season_suffix(item_id)
        if season_number is not None:
            return None
        content = await resolve_group_content(group_key)
        if content is None or content.form != "movie":
            return None
        per_provider = resolve_group(group_key)
        if per_provider is None:
            return None
        provider_id, result = next(iter(per_provider.items()))
        _, _, external_id = result.id.partition(":")
    else:
        # Provider-scoped episode wire id — split the prefix and hand the
        # suffix straight to stream(), exactly like /api/stream/{id}.
        provider_id, _, external_id = item_id.partition(":")
        if provider_id not in PROVIDERS or not external_id:
            return None

    provider = PROVIDERS[provider_id]
    http = get_client()
    try:
        stream = await provider.stream(external_id, None, http)
        TRACKER.record(provider_id, ok=True)
        return stream
    except ProviderError as e:
        # A `gated` verdict is client-side semantics, NOT an upstream
        # failure (ADR-0002 amendment): the item is deliberately
        # unavailable — degrade to the standing 404 without marking
        # the provider down.
        if e.code == "gated":
            log.info("jellyfin playback gated provider=%s id=%s", provider_id, item_id)
            return None
        log.warning("jellyfin playback stream failed provider=%s id=%s err=%s",
                    provider_id, item_id, e)
        TRACKER.record(provider_id, ok=False)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("jellyfin playback stream failed provider=%s id=%s err=%s",
                    provider_id, item_id, e)
        TRACKER.record(provider_id, ok=False)
        return None


def _container_from_type(stream_type: str) -> str:
    """StreamResponse.type → Jellyfin ``Container`` (D6).

    The native types are already Jellyfin container strings (``mp4``,
    ``m3u8``, ``hls``, ``dash``); pass them through verbatim rather than
    inventing a second mapping that could disagree.
    """
    return stream_type


@router.get(
    "/Items/{item_id:path}/PlaybackInfo",
    response_model=PlaybackInfoResponse, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
@router.post(
    "/Items/{item_id:path}/PlaybackInfo",
    response_model=PlaybackInfoResponse, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def playback_info(item_id: str) -> PlaybackInfoResponse:
    """PlaybackInfo: one thin MediaSource per playable item (D6).

    The @jellyfin/sdk hits this with POST (capture row 6) and the spec
    declares GET; both spellings serve the identical envelope. The
    container is learned from the provider's actual ``StreamResponse`` —
    one upstream ``stream()`` call, the same cost a native client pays
    for ``/api/stream``. ``Path`` is fictitious (bytes always come from
    ``/Videos/{id}/stream``); ``PlaySessionId`` is a fresh UUID. Unplayed
    ids 404 (D2); a series/season card is not playable and 404s too.
    """
    stream = await _resolve_stream(item_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    play_session_id = str(uuid.uuid4())
    source = MediaSourceInfo(
        Id=item_id,
        Container=_container_from_type(stream.type),
        Path=f"/Videos/{item_id}/stream",
        PlaySessionId=play_session_id,
    )
    return PlaybackInfoResponse(MediaSources=[source], PlaySessionId=play_session_id)


@router.get(
    "/Items/{item_id:path}/Similar",
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def item_similar(
    item_id: str,
    limit: int = Query(default=12),
) -> BaseItemDtoQueryResult:
    """Similar-shelf — same-genre cards from the cached snapshot (#218).

    The app fires this on every movie/series detail page; it used to
    answer a deliberately empty shelf. With genre metadata (#213) the
    snapshot can answer it: cards sharing at least one genre with the
    item, in the same Movie/Series + g2: + ImageTags shape as the view
    grid, the item itself excluded, capped at ``limit`` (the client asks
    for 12). A genre-less item or a cold snapshot stays an empty shelf.

    The full ``BaseItemDtoQueryResult`` envelope is required: Switchfin
    parses every list response as ``Result<T>`` with
    ``NLOHMANN_JSON_FROM`` (no defaults), so a missing ``StartIndex``
    raised ``out_of_range.403`` on the console.
    """
    wanted = set(_genres_for_group(item_id))
    if not wanted:
        return BaseItemDtoQueryResult()
    server_id = _server_id()
    dtos: list[BaseItemDto] = []
    seen: set[str] = set()
    for row, it in _home_items():
        if it.group_key == item_id or it.group_key in seen:
            continue
        if not (set(it.genres) & wanted):
            continue
        seen.add(it.group_key)
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


#: ``StreamResponse.type`` → the ``Content-Type`` the client expects (D7).
_STREAM_CTYPE = {
    "mp4": "video/mp4",
    "m3u8": "application/vnd.apple.mpegurl",
    "hls": "application/vnd.apple.mpegurl",
}

#: ``URI="..."`` attributes inside an HLS manifest (EXT-X-KEY, EXT-X-MAP,
#: EXT-X-MEDIA, child playlists) — rewritten like every plain segment line.
_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')

_MAX_PROXY_HOPS = 5

#: Per-item memo of the provider headers + CDN host the byte/segment proxy
#: must use. The manifest is fetched once; segments arrive en masse during
#: playback, and re-running the provider's ``stream()`` (one upstream
#: scrape per call) for every segment would hammer the provider. A short
#: TTL memo fuels segments; expiry falls back to one re-resolution. It
#: deliberately memoizes the provider HEADER MAP and the chosen CDN host
#: — NOT the upstream URL, which stays ADR-0003's "never cached"
#: (session-scoped/token-signed); the fresh ``stream()`` on expiry is
#: exactly the "a miss costs one request" cost the ADR accepts.
_STREAM_MEMO: dict[str, tuple[float, str, dict[str, str], frozenset[str]]] = {}
_STREAM_MEMO_TTL_S = 15 * 60


def _cdn_host(url: str) -> str | None:
    """The lowercase hostname of an http(s) URL, or None."""
    host = urlparse(url).hostname
    return host.lower() if host else None


def _registrable_domain(host: str) -> str:
    """The last two labels of a hostname — the anchor the stream proxy's
    CDN check compares against.

    Live HLS trees routinely hand child playlists to a SIBLING subdomain
    of the same domain (``api.unimay.media`` hands the tree to
    ``cdn.unimay.media``), and a two-label anchor is exactly stable
    across those siblings while still excluding foreign registrants:
    ``evil.example`` can never equal ``cdn.example``. Co.UK-style
    multi-label public suffixes are out of scope — no UA provider CDN
    uses one.
    """
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def _stream_target_allowed(url: str, cdn_host: str, allowed: frozenset[str] = frozenset()) -> bool:
    """Whether the byte proxy may reach ``url``: a host on the same
    registrable domain as the CDN the provider selected for the item
    (``cdn.example`` and its ``media.``/``api.`` siblings are one CDN),
    or a registrable domain the provider explicitly sanctioned in
    ``StreamResponse.allowed_domains`` (a 302 gateway may hand bytes to
    a foreign CDN the provider picked, e.g. ufdub episodes on Dropbox).

    This is the stream proxy's standing posture: the facade fetches bytes
    only from the CDN a provider picked — a sibling subdomain of the same
    domain is still the picked CDN, a foreign registrable domain is not
    (unless provider-sanctioned), and a client pointing the segment route
    at an arbitrary host fails closed (mirrors the poster proxy's
    allowlist).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname is None:
        return False
    host = parsed.hostname.lower()
    return _registrable_domain(host) == _registrable_domain(cdn_host) or _registrable_domain(host) in allowed


def _is_hls_stream(stream: StreamResponse) -> bool:
    return stream.type in ("m3u8", "hls") or stream.url.endswith(".m3u8")


def _segment_url(item_id: str, upstream: str) -> str:
    """Backend URL a rewritten media reference re-enters through."""
    return f"/Videos/{item_id}/segment?url={quote(upstream, safe='')}"


def _rewrite_m3u8(body: str, manifest_url: str, item_id: str) -> str:
    """Rewrite a fetched manifest so every media reference re-enters the
    backend: plain segment/child-playlist lines AND ``URI="..."``
    attributes (key files, EXT-X-MAP init segments) become
    ``/Videos/{item_id}/segment`` fetches. Relative references resolve
    against the manifest URL (urljoin) — the client only ever talks to
    the backend, which owns the provider headers."""
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("#"):
            lines.append(
                _URI_ATTR_RE.sub(
                    lambda m: f'URI="{_segment_url(item_id, urljoin(manifest_url, m.group(1)))}"',
                    line,
                )
            )
        else:
            lines.append(_segment_url(item_id, urljoin(manifest_url, stripped)))
    return "\n".join(lines) + "\n"


def _memo_stream(
    item_id: str, cdn_host: str, headers: dict[str, str], allowed: frozenset[str]
) -> None:
    """Writer for the segment memo. Values are returned by reference, so
    the store hands out copies — never the live provider dict."""
    _STREAM_MEMO[item_id] = (time.monotonic(), cdn_host, dict(headers), allowed)


async def _proxy_target(item_id: str) -> tuple[str, dict[str, str], frozenset[str]] | None:
    """(cdn_host, provider headers, allowed domains) the segment proxy
    must use.

    Serves from the memo when fresh; otherwise re-resolves the stream
    once and memoizes. None → 404 (D2)."""
    hit = _STREAM_MEMO.get(item_id)
    if hit is not None and time.monotonic() - hit[0] < _STREAM_MEMO_TTL_S:
        return hit[1], dict(hit[2]), hit[3]
    stream = await _resolve_stream(item_id)
    if stream is None:
        return None
    cdn_host = _cdn_host(stream.url)
    if cdn_host is None:
        return None
    _memo_stream(item_id, cdn_host, stream.headers, stream.allowed_domains)
    return cdn_host, dict(stream.headers), stream.allowed_domains


async def _open_upstream(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    range_header: str | None,
    cdn_host: str,
    hops: int = 0,
    allowed: frozenset[str] = frozenset(),
) -> tuple[httpx.Response, Callable[[], Awaitable[None]]] | None:
    """Open a validating byte stream to ``url``, following redirects by
    hand and re-validating EVERY hop against the item's CDN host.

    Returns ``(resp, closer)`` where the closer releases the upstream
    stream once the caller is done feeding bytes; None fails closed
    (D2 posture — never raises). Only a 2xx response opens a stream: a
    403/416/500 CDN verdict is a playback-grade failure the facade cannot
    meaningfully relay, so it becomes the same 404 an unresolvable id
    gets.
    """
    if hops > _MAX_PROXY_HOPS or not _stream_target_allowed(url, cdn_host, allowed):
        return None
    req_headers = dict(headers)
    if range_header is not None:
        req_headers["Range"] = range_header
    try:
        cm = http.stream("GET", url, headers=req_headers)
        resp = await cm.__aenter__()
    except httpx.HTTPError:
        return None
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        await cm.__aexit__(None, None, None)
        if location is None:
            return None
        return await _open_upstream(
            http, urljoin(url, location), headers, range_header, cdn_host, hops + 1, allowed
        )
    if resp.status_code < 200 or resp.status_code >= 300:
        await cm.__aexit__(None, None, None)
        return None

    async def _closer() -> None:
        await cm.__aexit__(None, None, None)

    return resp, _closer


async def _fetch_manifest(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    cdn_host: str,
    hops: int = 0,
    allowed: frozenset[str] = frozenset(),
) -> httpx.Response | None:
    """Fetch a small HLS manifest with hop revalidation; only a 200 counts."""
    if hops > _MAX_PROXY_HOPS or not _stream_target_allowed(url, cdn_host, allowed):
        return None
    try:
        resp = await http.get(url, headers=headers)
    except httpx.HTTPError:
        return None
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        if location is None:
            return None
        return await _fetch_manifest(
            http, urljoin(url, location), headers, cdn_host, hops + 1, allowed
        )
    if resp.status_code != 200:
        log.warning("jellyfin manifest non-200 url=%s status=%s", url, resp.status_code)
        return None
    return resp


def _streaming_response(
    resp: httpx.Response,
    close: Callable[[], Awaitable[None]],
    fallback_ctype: str,
) -> StreamingResponse:
    """Wrap an upstream byte stream as the facade's response.

    Upstream's own status, ``Content-Type``, ``Content-Range`` and
    ``Accept-Ranges`` ride along (a file proxy must answer a ``Range``
    with the CDN's 206 honestly); only a missing Content-Type falls back
    to the provider type's expected value. The upstream stream is released
    when the response body finishes — or when the client disconnects.
    """
    out_headers: dict[str, str] = {}
    for name in ("Content-Type", "Content-Range", "Accept-Ranges"):
        value = resp.headers.get(name)
        if value is not None:
            out_headers[name] = value
    if "Content-Type" not in out_headers:
        out_headers["Content-Type"] = fallback_ctype

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await close()

    return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers)


@router.get("/Videos/{item_id:path}/stream")
async def video_stream(item_id: str, request: Request) -> Response:
    """Conditional stream handler (D7): redirect, or the byte proxy.

    ``StreamResponse`` with no header map → 302 straight to the CDN URL
    (no proxying). With a header map the backend owns the bytes: mp4
    files forward the client's ``Range`` and echo the CDN's
    206/Content-Range/Accept-Ranges back; HLS manifests are fetched (with
    the provider's headers), every segment/``URI=`` reference rewritten to
    ``/Videos/{id}/segment``, and served as the mpegurl content type — so
    the client's segments stay behind the facade too.
    """
    stream = await _resolve_stream(item_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    if not stream.headers:
        return RedirectResponse(stream.url, status_code=302)

    cdn_host = _cdn_host(stream.url)
    if cdn_host is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    _memo_stream(item_id, cdn_host, stream.headers, stream.allowed_domains)
    http = get_client()

    if _is_hls_stream(stream):
        manifest = await _fetch_manifest(
            http, stream.url, stream.headers, cdn_host, allowed=stream.allowed_domains
        )
        if manifest is None:
            raise HTTPException(status_code=404, detail="item_unavailable")
        body = _rewrite_m3u8(
            manifest.content.decode("utf-8", errors="replace"), stream.url, item_id
        )
        # Whatever the upstream claims, a served manifest is a playlist
        # body and gets the mpegurl content-type (D7). If the provider
        # ever mislabels type (mp4) on a .m3u8 URL, the playlist
        # detection above decides, so ctype must not follow the label.
        return Response(content=body, media_type=_STREAM_CTYPE["m3u8"])

    opened = await _open_upstream(
        http,
        stream.url,
        stream.headers,
        request.headers.get("range"),
        cdn_host,
        allowed=stream.allowed_domains,
    )
    if opened is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    upstream, closer = opened
    return _streaming_response(
        upstream, closer, _STREAM_CTYPE.get(stream.type, "application/octet-stream")
    )


@router.get("/Videos/{item_id:path}/segment")
async def video_segment(item_id: str, url: str = Query(...)) -> Response:
    """Proxy one rewritten HLS reference (D7).

    ``url`` is an upstream reference embedded by ``_rewrite_m3u8`` (already
    percent-encoded, decoded once by FastAPI): an ordinary segment, or
    another playlist — a master's variant, or a variant's own segment
    list. Segment bytes flow through the byte proxy; a playlist reference
    is fetched and re-rewritten exactly like the top manifest, so a
    multi-level playlist tree keeps every descendant reference pointed at
    the backend (the client's requests always carry the provider headers).
    The host must match the item's CDN host (dot-boundary) — anything
    else fails closed to 404 — and Referer-gated CDNs still serve.
    """
    target = await _proxy_target(item_id)
    if target is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    cdn_host, headers, allowed = target
    http = get_client()

    if url.rstrip("/").lower().endswith(".m3u8"):
        manifest = await _fetch_manifest(http, url, headers, cdn_host, allowed=allowed)
        if manifest is None:
            raise HTTPException(status_code=404, detail="item_unavailable")
        body = _rewrite_m3u8(
            manifest.content.decode("utf-8", errors="replace"), url, item_id
        )
        return Response(content=body, media_type=_STREAM_CTYPE["m3u8"])

    opened = await _open_upstream(http, url, headers, None, cdn_host, allowed=allowed)
    if opened is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    upstream, closer = opened
    return _streaming_response(upstream, closer, "application/octet-stream")


# ------------------------------------------------------------ user state (#257)


@router.post(
    "/Users/{user_id}/FavoriteItems/{item_id}",
    response_model=UserDataResult, response_model_exclude_none=True,
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
    response_model=UserDataResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def favorite_remove(user_id: str, item_id: str) -> UserDataResult:
    """Un-favorite an item (spec #257) — same response contract."""
    set_favorite(item_id, False)
    return _user_data(item_id)  # type: ignore[return-value]


@router.post(
    "/Users/{user_id}/PlayedItems/{item_id}",
    response_model=UserDataResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def played_add(user_id: str, item_id: str) -> UserDataResult:
    """Mark an item played (spec #257) — the context-menu affordance."""
    set_played(item_id, True)
    return _user_data(item_id)  # type: ignore[return-value]


@router.delete(
    "/Users/{user_id}/PlayedItems/{item_id}",
    response_model=UserDataResult, response_model_exclude_none=True,
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
    "/LiveTv/Channels",
    response_model=BaseItemDtoQueryResult, response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def live_tv_channels() -> BaseItemDtoQueryResult:
    """Live TV tab (spec #257): an empty channel listing — not a 404.

    There is no live source (out of scope), so the channel list is
    honestly empty; Switchfin's Live TV tab renders it without errors.
    """
    return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)


@router.post("/Sessions/Playing", dependencies=[Depends(require_token)])
async def sessions_playing(request: Request) -> Response:
    """Playback-start report (D8): accept, answer 204, record position.

    The @jellyfin/sdk posts a full PlaybackStartInfo body here the moment
    playback starts (capture row 6); the position seeds the resume shelf
    (ticket #214; persisted per #248).
    """
    await _record_playback_from(request, flush=False)
    return Response(status_code=204)


@router.post("/Sessions/Progress", dependencies=[Depends(require_token)])
@router.post("/Sessions/Playing/Progress", dependencies=[Depends(require_token)])
async def sessions_progress(request: Request) -> Response:
    """Playback-progress report (D8): accept, answer 204, record position.

    Heartbeats update the stored position (debounced write, #248); the
    newest report wins.
    """
    await _record_playback_from(request, flush=False)
    return Response(status_code=204)


@router.post("/Sessions/Stopped", dependencies=[Depends(require_token)])
@router.post("/Sessions/Playing/Stopped", dependencies=[Depends(require_token)])
async def sessions_stopped(request: Request) -> Response:
    """Playback-stop report (D8): accept, answer 204, record the stop
    position — the final value the resume shelf shows (ticket #214).
    Flushed to the state file immediately (#248), so the position
    survives a restart.
    """
    await _record_playback_from(request, flush=True)
    return Response(status_code=204)


@router.post("/Sessions/Logout", dependencies=[Depends(require_token)])
async def sessions_logout() -> Response:
    """Session-end report (D8, capture verdict): accept, answer 204.

    The SDK's ``reportSessionEnded`` treats the logout call as the
    SignedOut signal and halts on anything but a 204, so the facade must
    answer exactly that. No token/session state is dropped — there is
    none to drop.
    """
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


__all__ = ["require_token", "router"]
