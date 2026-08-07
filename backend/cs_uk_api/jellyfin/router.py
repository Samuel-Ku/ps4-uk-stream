"""Jellyfin facade router (spec #100, tickets #102 + #104).

Mounted on the existing FastAPI app at the Jellyfin paths — deliberately
NOT under ``/api/*``, so the native contract is untouched and a Jellyfin
client pointed at ``host:port`` finds a server without configuration.

Ticket #102: the handshake. Ticket #104: the catalog surface — views,
item listing, poster. Ticket #105: item detail + hierarchy. Ticket
#106: PlaybackInfo. Ticket #107: the conditional stream handler
(``GET /Videos/{id}/stream``) with byte proxying, Range support, and
HLS segment rewriting. Ticket #108: sessions no-op endpoints
(``/Sessions/Playing|Progress|Stopped|Logout`` → 204), all behind the
same ``require_token`` gate.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..catalog_state import get_home, load_home, resolve_group, resolve_group_content
from ..config import SETTINGS
from ..health import TRACKER
from ..http_client import get_client
from ..models import ContentResponse, Episode, HomeItem, HomeRow, Season, StreamResponse
from ..providers import PROVIDERS
from .auth import require_token
from .models import (
    AuthenticationResult,
    BaseItemDto,
    BaseItemDtoQueryResult,
    MediaSourceInfo,
    PlaybackInfoResponse,
    SystemInfoPublic,
    UserDto,
)

log = logging.getLogger("cs_uk_api.jellyfin")

router = APIRouter(tags=["jellyfin"])

#: What the server tells the client it is. The real Jellyfin server name
#: is configurable; we surface the project's own identity so the
#: client's connection dialog shows something recognizable.
_PRODUCT = "cs-uk-api"
_VERSION = "0.1.0"


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
_VIEW_TYPES = ("newest", "popular", "movie", "series", "anime", "cartoon", "dorama")

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
}

#: Home ``MediaType`` → Jellyfin item Type. Only Movie/Series are expressible
#: on the wire (AC: "correct Type (Movie/Series)"); style-tagged rows are
#: episodic content and become Series.
_JF_TYPE_BY_ROW = {
    "movie": "Movie",
    "series": "Series",
    "anime": "Series",
    "cartoon": "Series",
    "dorama": "Series",
}


def _poster_tag(poster_url: str) -> str:
    """Opaque ``ImageTags.Primary`` value (D9).

    Deterministic in the poster URL, so a client-side image cache
    busts exactly when the upstream art changes and not otherwise.
    """
    return hashlib.sha256(poster_url.encode()).hexdigest()[:16]


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
    """One library card: Movie/Series item carrying the ``g1:`` id.

    ``ImageTags.Primary`` is set only when the card carries a poster
    (D9). ``year`` is surfaced as ``ProductionYear`` (Jellyfin's field);
    ``ParentId`` is the view the card came from.
    """
    dto = BaseItemDto(
        Name=item.title,
        ServerId=server_id,
        Id=item.group_key,
        Type=_JF_TYPE_BY_ROW.get(item.type, "Series"),
        ProductionYear=item.year,
        ParentId=_VIEW_ID_BY_TYPE[row.type],
    )
    if item.poster is not None:
        dto.ImageTags = {"Primary": _poster_tag(item.poster)}
    return dto


def _poster_for(item_id: str) -> str | None:
    """The canonical poster URL for a ``g1:`` item id, or None.

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
    """The view id that surfaced a ``g1:`` item, from the cached home.

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
    is the stateless ``g1:`` group key, so the client's bookmarks and
    the native route agree.
    """
    dto = BaseItemDto(
        Name=content.title,
        ServerId=server_id,
        Id=group_key,
        Type="Movie" if content.type == "movie" else "Series",
        ProductionYear=content.year,
        Overview=content.description,
    )
    parent = _view_id_for_item(group_key)
    if parent is not None:
        dto.ParentId = parent
    poster = _poster_for(group_key)
    if poster is not None:
        dto.ImageTags = {"Primary": _poster_tag(poster)}
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


class AuthenticateByNameRequest(BaseModel):
    """Login body. Any username/password completes the handshake (D4)."""

    Username: str = ""
    Pw: str = ""


@router.get("/System/Info/Public", response_model=SystemInfoPublic)
async def system_info_public() -> SystemInfoPublic:
    """Server discovery: what a client hits first when adding the server.

    Unauthenticated by design (D4): the client needs this to render the
    login screen at all.
    """
    return SystemInfoPublic(
        LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
        SystemName=_PRODUCT,
        Version=_VERSION,
        ProductName=_PRODUCT,
        StartupWizardCompleted=True,
        Id=_server_id(),
    )


@router.get("/System/Info", response_model=SystemInfoPublic, dependencies=[Depends(require_token)])
async def system_info(
    _token: str = Depends(require_token),
) -> SystemInfoPublic:
    """Full server info — authenticated in real Jellyfin.

    A client that has completed the handshake fetches this to confirm
    the server identity; the web UI reads ``SystemName``/``Version`` off
    it when reconnecting to a cached server. The first private facade
    route: proves the ``require_token`` gate on a real endpoint.
    """
    return SystemInfoPublic(
        LocalAddress=f"{SETTINGS.host}:{SETTINGS.port}",
        SystemName=_PRODUCT,
        Version=_VERSION,
        ProductName=_PRODUCT,
        StartupWizardCompleted=True,
        Id=_server_id(),
    )


@router.post("/Users/AuthenticateByName", response_model=AuthenticationResult)
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
        Configuration=None,
        Policy=None,
        PrimaryImageTag=None,
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
    dependencies=[Depends(require_token)],
)
async def user_views_server(user_id: str) -> BaseItemDtoQueryResult:
    """Server-style spelling of the views call (spec D5)."""
    return await _user_views()


@router.get(
    "/Items",
    response_model=BaseItemDtoQueryResult,
    dependencies=[Depends(require_token)],
)
async def items_listing(
    parent_id: str | None = Query(default=None, alias="parentId"),
    user_id: str | None = Query(default=None, alias="userId"),
) -> BaseItemDtoQueryResult:
    """Library listing for one view, OR children of a series/season
    (ticket #105 hierarchy, D3).

    Two parent kinds are served by the same route:

      - ``parentId`` = a view's ``Id`` (echoed from ``/UserViews``) —
        the home-row cards, exactly the ticket #104 behaviour.
      - ``parentId`` = a series' ``g1:`` group key → the season list
        (``Type: Season``). ``parentId`` = a ``<group_key>:S<n>`` season
        id → the season's episodes (``Type: Episode``).

    Unknown or absent view → empty result (Jellyfin's tolerant answer
    for a stale parent, D5). Cold resolution cache or a movie parent →
    empty (a movie has no children, D3; episodes survive only under a
    resolved season).
    """
    row_type = _VIEW_TYPE_BY_ID.get(parent_id or "")
    if row_type is None:
        return await _hierarchy(parent_id)
    home = await load_home()
    server_id = _server_id()
    for row in home.rows:
        if row.type == row_type:
            dtos = [_item_dto(row, it, server_id) for it in row.items]
            return BaseItemDtoQueryResult(Items=dtos, TotalRecordCount=len(dtos))
    # A valid view id whose row is currently absent (e.g. «Популярні
    # зараз» when no provider carries it) is an empty library, not an
    # error — same tolerant answer as an unknown parent.
    return BaseItemDtoQueryResult(Items=[], TotalRecordCount=0)


def _split_season_suffix(parent_id: str) -> tuple[str, int | None]:
    """(group_key, season_number) for a season id, else (as-is, None).

    Season ids are ``<group_key>:S<n>`` (D2); the group key never
    carries an ``:S<n>`` tail, so ``rpartition`` cleanly separates the
    trailing season marker. A series/movie group key returns itself.
    """
    if not parent_id.startswith("g1:"):
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
    response_model=BaseItemDto,
    dependencies=[Depends(require_token)],
)
async def item_detail(item_id: str) -> BaseItemDto:
    """Item detail (ticket #105, D2/D3): resolve a ``g1:`` key to its
    ContentResponse via the shared resolution map, and return a
    Movie/Series DTO.

    Unresolvable ids 404 with the same "item unavailable" verdict as a
    cold resolution cache (D2): ``g1:`` keys not in the cached home, and
    episode ids — served through the season listing, not reverse-
    resolvable on their own.
    """
    # Episode wire ids (``p1:s1e1``) are not reverse-resolvable: there is
    # no group key in them. They are served exclusively through the
    # season hierarchy, so /Items/{id} answers 404 for them.
    if not item_id.startswith("g1:"):
        raise HTTPException(status_code=404, detail="item_unavailable")
    group_key, season_number = _split_season_suffix(item_id)
    content = await resolve_group_content(group_key)
    if content is None:
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
    dependencies=[Depends(require_token)],
)
async def item_primary_image(item_id: str) -> RedirectResponse:
    """Poster art (D9): 302 to the existing poster proxy.

    ``maxWidth``/``tag`` and friends are ignored — the original image
    is served, no resizing in v1. Unknown item or poster-less item →
    404; Jellyfin clients render a placeholder instead of an image.
    """
    poster_url = _poster_for(item_id)
    if poster_url is None:
        raise HTTPException(status_code=404, detail="poster_unavailable")
    return RedirectResponse(
        url=f"/api/poster?u={quote(poster_url, safe='')}",
        status_code=302,
    )


async def _resolve_stream(item_id: str) -> StreamResponse | None:
    """The upstream ``StreamResponse`` behind a playable item id, or None.

    Resolves the two playable id families (D2/D3) to their provider, then
    runs ``provider.stream()`` exactly as the native ``/api/stream/{id}``
    route does — same bare external ids, ``translation=None`` (default
    voice), same shared ``httpx`` client:

      - a movie's ``g1:`` group key → the group's first-seen provider
        (the same provider the detail page shows first), whose BARE
        external id is what stream() consumes. Playability is decided on
        the content's FORM — ``content.type == "movie"`` — the same
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
    if item_id.startswith("g1:"):
        # Series/season keys and cold groups: not playable on their own.
        group_key, season_number = _split_season_suffix(item_id)
        if season_number is not None:
            return None
        content = await resolve_group_content(group_key)
        if content is None or content.type != "movie":
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
    "/Items/{item_id}/PlaybackInfo",
    response_model=PlaybackInfoResponse,
    dependencies=[Depends(require_token)],
)
@router.post(
    "/Items/{item_id}/PlaybackInfo",
    response_model=PlaybackInfoResponse,
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
        Path=f"/videos/{item_id}",
        PlaySessionId=play_session_id,
    )
    return PlaybackInfoResponse(MediaSources=[source], PlaySessionId=play_session_id)


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
_STREAM_MEMO: dict[str, tuple[float, str, dict[str, str]]] = {}
_STREAM_MEMO_TTL_S = 15 * 60


def _cdn_host(url: str) -> str | None:
    """The lowercase hostname of an http(s) URL, or None."""
    host = urlparse(url).hostname
    return host.lower() if host else None


def _stream_target_allowed(url: str, cdn_host: str) -> bool:
    """Whether the byte proxy may reach ``url``: only the CDN host the
    provider selected for the item, dot-boundary (subdomains allowed).

    This is the stream proxy's standing posture: the facade fetches bytes
    only from the CDN a provider picked, never from arbitrary hosts a
    client would point it at (mirrors the poster proxy's allowlist, but
    scoped to the ONE host a stream owns).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname is None:
        return False
    host = parsed.hostname.lower()
    return host == cdn_host or host.endswith("." + cdn_host)


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


def _memo_stream(item_id: str, cdn_host: str, headers: dict[str, str]) -> None:
    """Writer for the segment memo. Values are returned by reference, so
    the store hands out copies — never the live provider dict."""
    _STREAM_MEMO[item_id] = (time.monotonic(), cdn_host, dict(headers))


async def _proxy_target(item_id: str) -> tuple[str, dict[str, str]] | None:
    """(cdn_host, provider headers) the segment proxy must use.

    Serves from the memo when fresh; otherwise re-resolves the stream
    once and memoizes. None → 404 (D2)."""
    hit = _STREAM_MEMO.get(item_id)
    if hit is not None and time.monotonic() - hit[0] < _STREAM_MEMO_TTL_S:
        return hit[1], dict(hit[2])
    stream = await _resolve_stream(item_id)
    if stream is None:
        return None
    cdn_host = _cdn_host(stream.url)
    if cdn_host is None:
        return None
    _memo_stream(item_id, cdn_host, stream.headers)
    return cdn_host, dict(stream.headers)


async def _open_upstream(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    range_header: str | None,
    cdn_host: str,
    hops: int = 0,
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
    if hops > _MAX_PROXY_HOPS or not _stream_target_allowed(url, cdn_host):
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
            http, urljoin(url, location), headers, range_header, cdn_host, hops + 1
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
) -> httpx.Response | None:
    """Fetch a small HLS manifest with hop revalidation; only a 200 counts."""
    if hops > _MAX_PROXY_HOPS or not _stream_target_allowed(url, cdn_host):
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
            http, urljoin(url, location), headers, cdn_host, hops + 1
        )
    if resp.status_code != 200:
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


@router.get("/Videos/{item_id}/stream", dependencies=[Depends(require_token)])
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
    _memo_stream(item_id, cdn_host, stream.headers)
    http = get_client()

    if _is_hls_stream(stream):
        manifest = await _fetch_manifest(http, stream.url, stream.headers, cdn_host)
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
        http, stream.url, stream.headers, request.headers.get("range"), cdn_host
    )
    if opened is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    upstream, closer = opened
    return _streaming_response(
        upstream, closer, _STREAM_CTYPE.get(stream.type, "application/octet-stream")
    )


@router.get("/Videos/{item_id}/segment", dependencies=[Depends(require_token)])
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
    cdn_host, headers = target
    http = get_client()

    if url.rstrip("/").lower().endswith(".m3u8"):
        manifest = await _fetch_manifest(http, url, headers, cdn_host)
        if manifest is None:
            raise HTTPException(status_code=404, detail="item_unavailable")
        body = _rewrite_m3u8(
            manifest.content.decode("utf-8", errors="replace"), url, item_id
        )
        return Response(content=body, media_type=_STREAM_CTYPE["m3u8"])

    opened = await _open_upstream(http, url, headers, None, cdn_host)
    if opened is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    upstream, closer = opened
    return _streaming_response(upstream, closer, "application/octet-stream")


@router.post("/Sessions/Playing", dependencies=[Depends(require_token)])
async def sessions_playing() -> Response:
    """Playback-start report (D8): accept, answer 204, store nothing.

    The @jellyfin/sdk posts a full PlaybackStartInfo body here the moment
    playback starts (capture row 6). The facade has no session state and
    keeps none — the response exists so the client's report lands.
    """
    return Response(status_code=204)


@router.post("/Sessions/Progress", dependencies=[Depends(require_token)])
async def sessions_progress() -> Response:
    """Playback-progress report (D8): accept, answer 204, store nothing."""
    return Response(status_code=204)


@router.post("/Sessions/Stopped", dependencies=[Depends(require_token)])
async def sessions_stopped() -> Response:
    """Playback-stop report (D8): accept, answer 204, store nothing.

    Resume/history are out of scope (D8) — this is where a real server
    would persist the stop position; the facade forgets it on purpose.
    """
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


__all__ = ["require_token", "router"]