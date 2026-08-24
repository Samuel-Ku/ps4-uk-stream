"""DTO mapping for the Jellyfin facade (ticket #344).

The serialization half of the facade: pure mapping functions that turn
plain domain data (home rows, snapshot cards, content pages, merged
search groups) into the Jellyfin wire DTOs. Every cache/snapshot LOOKUP
(``_card_for_group``, ``_poster_for``, ``_genres_for_group``,
``_year_for_group``, ``_view_id_for_item``…) stays in the router — this
module never scans state; the caller resolves first and passes the
resolved values in. The same goes for id grammar (``_episode_wire_id``,
view-id uuid5s): callers hand in finished wire ids.

Wire-shape rules pinned here (see CONTEXT.md / the D-numbers): the
``ImageTags.Primary`` tag is present IFF a poster URL exists (D9) and is
a deterministic hash of that URL (client-side cache busting); UserData
derives percentage only when a runtime is known (spec #257); detail DTOs
carry the download-source entry the client's download manager names
files by (spec #280).
"""

from __future__ import annotations

import hashlib
import re

from ..models import (
    ContentResponse,
    Episode,
    HomeItem,
    SearchGroup,
    Season,
)
from ..row_kinds import ROW_KINDS
from .models import (
    BaseItemDto,
    ItemCounts,
    MediaSourceInfo,
    PersonDto,
    SearchHint,
    UserDataResult,
)


def poster_tag(poster_url: str) -> str:
    """Opaque ``ImageTags.Primary`` value (D9).

    Deterministic in the poster URL, so a client-side image cache
    busts exactly when the upstream art changes and not otherwise.
    """
    return hashlib.sha256(poster_url.encode()).hexdigest()[:16]


def user_data(
    item_id: str | None,
    *,
    favorite: bool,
    played: bool,
    position_ticks: int | None = None,
    runtime_ticks: int | None = None,
) -> UserDataResult | None:
    """The UserDataResult shape for an item's store state (spec #257).

    The caller resolves IsFavorite/Played and the playback position
    through the stores; this function only shapes the wire object:
    PlayedPercentage derives from the position/runtime when known, else
    100 when played and 0 otherwise. None when there is no id to look up
    (a view row, a season without a concrete item).
    """
    if item_id is None:
        return None
    result = UserDataResult(IsFavorite=favorite, Played=played)
    if position_ticks is not None:
        result.PlaybackPositionTicks = position_ticks
        if runtime_ticks and runtime_ticks > 0:
            result.PlayedPercentage = round(min(100.0, position_ticks / runtime_ticks * 100), 2)
        elif result.Played:
            result.PlayedPercentage = 100.0
    elif result.Played:
        result.PlayedPercentage = 100.0
    result.PlayCount = 1 if result.Played else 0
    return result


def row_dto(
    title: str,
    server_id: str,
    *,
    view_id: str,
    collection_type: str | None,
) -> BaseItemDto:
    """One virtual library (D5): a ``CollectionFolder`` whose ``Id`` the
    client echoes back as ``parentId`` on ``/Items``."""
    return BaseItemDto(
        Name=title,
        ServerId=server_id,
        Id=view_id,
        Type="CollectionFolder",
        CollectionType=collection_type,
    )


def item_dto(
    item: HomeItem,
    server_id: str,
    *,
    jf_type: str,
    parent_view_id: str,
    user_data_value: UserDataResult | None,
) -> BaseItemDto:
    """One library card: Movie/Series item carrying the ``g2:`` id.

    The caller resolves the item's WIRE TYPE (the card's form
    re-verified against cached content when one exists — ticket #216),
    the view the card came from, and its UserData.

    ``ImageTags.Primary`` is set only when the card carries a poster
    (D9). ``year`` is surfaced as ``ProductionYear`` (Jellyfin's field);
    ``ParentId`` is the view the card came from.
    """
    dto = BaseItemDto(
        Name=item.title,
        ServerId=server_id,
        Id=item.group_key,
        Type=jf_type,
        ProductionYear=item.year,
        ParentId=parent_view_id,
        # Spec #257: hearts/played checkmarks/progress render from UserData.
        UserData=user_data_value,
    )
    if item.poster is not None:
        dto.ImageTags = {"Primary": poster_tag(item.poster)}
    return dto


def item_counts(*, movies: int, series: int, episodes: int) -> ItemCounts:
    """Library-size counts envelope (spec #280). ``ItemCount`` is the
    movie+series sum, the number the dashboard headline shows; the
    episode total arrives pre-counted from the CACHED content pages."""
    return ItemCounts(
        MovieCount=movies,
        SeriesCount=series,
        EpisodeCount=episodes,
        ItemCount=movies + series,
    )


def safe_filename(title: str) -> str:
    """A filename-safe rendering of a title (spec #280)."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", title).strip("_")
    return cleaned or "item"


def attach_download_source(dto: BaseItemDto, title: str) -> None:
    """Give a detail DTO the download-source entry (spec #280).

    The client's download manager reads ``MediaSources[].Name`` for the
    saved file's path; without it the Download button has no name to
    write. The source is deliberately minimal — just the stable
    title-based file name (the actual container is learned at download
    time from the provider's stream, so ``Container`` is omitted here
    rather than guessed).
    """
    item_id = dto.Id or ""
    dto.MediaSources = [
        MediaSourceInfo(
            Id=item_id,
            Container="",
            Path=f"/Items/{item_id}/Download",
            PlaySessionId="",
            Name=f"{safe_filename(title)}.mp4",
        )
    ]


def card_detail_dto(
    group_key: str,
    card: HomeItem,
    server_id: str,
    *,
    parent_view_id: str | None,
    poster_url: str | None,
) -> BaseItemDto:
    """Degraded detail built purely from the home snapshot card (#224).

    The card-data counterpart of ``content_detail_dto``: a known card
    whose live ``content()`` resolution failed transiently still answers
    the detail with the card's own data — title, type, year, genres,
    poster tag, parent view — instead of a hard 404 that blanks the
    whole page mid-run. Same lookups the full detail falls back to
    (#219 genres, #220 year, D9 poster); the parent view and poster URL
    arrive resolved by the caller.
    """
    dto = BaseItemDto(
        Name=card.title,
        ServerId=server_id,
        Id=group_key,
        Type="Movie" if card.form == "movie" else "Series",
        ProductionYear=card.year,
        Genres=list(card.genres),
    )
    if parent_view_id is not None:
        dto.ParentId = parent_view_id
    if poster_url is not None:
        dto.ImageTags = {"Primary": poster_tag(poster_url)}
    # Spec #280: the download manager names the saved file from
    # ``MediaSources[].Name`` — a stable title-based name on the detail.
    attach_download_source(dto, card.title)
    return dto


def content_detail_dto(
    group_key: str,
    content: ContentResponse,
    server_id: str,
    *,
    fallback_year: int | None,
    fallback_genres: list[str],
    parent_view_id: str | None,
    poster_url: str | None,
    user_data_value: UserDataResult | None,
) -> BaseItemDto:
    """Movie/Series detail built from a resolved ContentResponse.

    ``ImageTags.Primary`` iff the poster route would serve the item a
    poster (D9): the caller passes the canonical poster URL (the SAME
    home-card poster ``/Items/{id}/Images/Primary`` resolves, falling
    back to ``content.poster``), so the tag and the route always agree
    (a card with no art means no tag AND a 404 image, never a dangling
    tag). Translations stay server-side — the wire carries no
    translation surface. The item id is the stateless ``g2:`` group key,
    so the client's bookmarks and the native route agree.

    ``fallback_year``/``fallback_genres`` are the snapshot-card facts the
    detail falls back to when the content page lacks them (#220 year,
    #213/#219 genres).
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
        ProductionYear=content.year if content.year is not None else fallback_year,
        Overview=content.description,
        # Ticket #213: the detail page renders a genre row when present
        # (Switchfin ``media_movie``/``media_series`` show labelGenres
        # iff non-empty). Ticket #219: the content page often does NOT
        # repeat the card's genres (ufdub lists them on the card only) —
        # fall back to the snapshot card's genres so the row renders
        # where the data exists.
        Genres=list(content.genres or fallback_genres),
    )
    # Ticket #221: the People rail renders from BaseItemDto.People —
    # populated when the resolved provider's content page exposed cast
    # (kinotron/uaserialspro actor lists, klontv JSON-LD). Empty people
    # stays an empty list; Switchfin hides the rail then.
    dto.People = [PersonDto(Id=p.id, Name=p.name, Role=p.role) for p in content.people]
    # Ticket #222: the rating badge renders from CommunityRating — set
    # when the provider exposed a real score (klontv's JSON-LD
    # aggregateRating); None stays omitted so the badge hides instead
    # of showing 0.
    dto.CommunityRating = content.rating
    if parent_view_id is not None:
        dto.ParentId = parent_view_id
    if poster_url is not None:
        dto.ImageTags = {"Primary": poster_tag(poster_url)}
    # Spec #257: the detail screen's heart reads UserData.
    dto.UserData = user_data_value
    # Spec #280: the download manager names the saved file from
    # ``MediaSources[].Name`` — a stable title-based name on the detail.
    attach_download_source(dto, content.title)
    return dto


def season_dto(group_key: str, season: Season, server_id: str, series_name: str) -> BaseItemDto:
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


def episode_dto(
    group_key: str,
    season: Season,
    episode: Episode,
    server_id: str,
    series_name: str,
    *,
    wire_id: str,
    user_data_value: UserDataResult | None,
) -> BaseItemDto:
    """One Episode satellite (D2: id keeps the provider-scoped episode
    suffix the PlaybackInfo/stream tickets consume — passed in as
    ``wire_id``; D3: ParentId = the owning season id).

    ``IndexNumber`` = number inside the season, ``ParentIndexNumber`` =
    the season number. No ``ImageTags`` (D9).
    """
    return BaseItemDto(
        Name=episode.title,
        ServerId=server_id,
        Id=wire_id,
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
        UserData=user_data_value,
    )


def search_card_dto(group: SearchGroup, server_id: str) -> BaseItemDto:
    """One search-result card (ticket #106): the merged group in the same
    listing shape as a home card (D5/D9) — ``g2:`` id, Movie/Series Type
    from the group's canonical type, ``ImageTags.Primary`` present *iff*
    the card has a poster.

    No ``ParentId``: a searched card is not tied to a home row — search
    covers the whole catalog, not a view.
    """
    dto = BaseItemDto(
        Name=group.title,
        ServerId=server_id,
        Id=group.group_key,
        # The group's form is a required movie|series axis (Model B),
        # both of which are table kinds — the Type derives from the
        # row-kind table (spec #362 B).
        Type=ROW_KINDS[group.form].jf_type,
        ProductionYear=group.year,
    )
    if group.poster is not None:
        dto.ImageTags = {"Primary": poster_tag(group.poster)}
    return dto


def search_hint(group: SearchGroup) -> SearchHint:
    """One search-box hint (ticket #106): the same merged card in the
    ``SearchHint`` shape ``/Search/Hints`` serves."""
    hint = SearchHint(
        ItemId=group.group_key,
        Id=group.group_key,
        Name=group.title,
        Type=ROW_KINDS[group.form].jf_type,
        ProductionYear=group.year,
    )
    if group.poster is not None:
        hint.ImageTags = {"Primary": poster_tag(group.poster)}
    return hint


def genre_shelf_entry(genre: str, server_id: str, child_count: int) -> BaseItemDto:
    """One genre row of ``/Genres`` (ticket #213).

    Genre wire shape (Switchfin ``jellyfin::Genres``): ``{Id, Name,
    ImageTags, ChildCount}``. Id == Name, matching Jellyfin's own
    convention (genre ids are the names) so the id round-trips as the
    ``genreIds`` filter when the user taps a genre.
    """
    return BaseItemDto(
        Name=genre,
        ServerId=server_id,
        Id=genre,
        ChildCount=child_count,
    )


__all__ = [
    "attach_download_source",
    "card_detail_dto",
    "content_detail_dto",
    "episode_dto",
    "genre_shelf_entry",
    "item_counts",
    "item_dto",
    "poster_tag",
    "row_dto",
    "safe_filename",
    "search_card_dto",
    "search_hint",
    "season_dto",
    "user_data",
]
