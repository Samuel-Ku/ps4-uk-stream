"""The playable-id → ``provider.stream()`` seam (spec #443).

ONE owner for the step both the Jellyfin facade and the native teleport
route cross when they need a playable stream:

  - :func:`resolve_playable_target` — the id grammar only. It resolves a
    playable wire id to its provider and BARE external id, without
    calling the upstream. The teleport route validates a candidate with
    it before spending a stream request.
  - :func:`resolve_stream` — the established facade seam, extracted from
    :mod:`jellyfin.delivery`: the two playable id families, the group's
    member walk, and the None-means-404 posture (D2: never a 5xx for
    unplayable ids). The provider call, translation gate, health
    recording and play attestation live behind
    :func:`cs_uk_api.play_request.attempt_play`, the shared play-request
    seam native ``/api/stream/{id}`` also crosses. ``raise_errors=True``
    is the teleport route's mode: a refused attempt re-raises the
    provider's typed error instead of degrading to None, so the caller
    can distinguish an unplayable id from a lane fault.

Why a neutral module: the teleport route needs the same provider call,
health semantics and headers as playback. Importing the facade from the
native surface would invert the dependency direction; a second copy of
``provider.stream()`` would let the two answers drift.

The two playable id families are unchanged (D2/D3):

  - a movie's ``g2:``/``g3:`` group key → the group's members in
    first-seen order, LED by the member whose content page answered
    (``group_content_member``: content resolution walks the group too,
    so the page may have come from a member other than the first; Q47),
    each called with its BARE external id. Playability is decided on the
    content's FORM — a film can carry an anime style without becoming
    non-playable.
  - a provider-scoped episode wire id → the provider is the id prefix;
    the episode suffix is handed to ``stream()`` exactly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .catalog import (
    WALKABLE_VERDICT_CODES,
    group_content_member,
    group_resolution,
    member_attempts,
    resolve_item,
)
from .http_client import get_client
from .models import PlaySurface, StreamResponse
from .play_request import attempt_play, attest_refusal, playable_target
from .wire_identity import is_group_key, split_season_key

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayableTarget:
    """One playable id's provider route, before any upstream call."""

    provider_id: str
    external_id: str


async def resolve_playable_target(item_id: str) -> PlayableTarget | None:
    """Resolve a playable wire id to its provider route, or None.

    Mirrors the two playable families of :func:`resolve_stream` but
    performs no upstream work for the provider-scoped branch. The group
    branch still resolves the group's content because "is this group a
    movie?" is not knowable from the key alone.
    """
    if is_group_key(item_id):
        # Series/season keys and cold groups: not playable on their own.
        group_key, season_number = split_season_key(item_id)
        if season_number is not None:
            return None
        content = (await resolve_item(group_key)).content
        if content is None or content.form != "movie":
            return None
        res = group_resolution(group_key)
        if res is None or not res.sources:
            return None
        attempts = member_attempts(res.sources, lead=group_content_member(group_key))
        if not attempts:
            return None
        provider_id, external_id = attempts[0]
        return PlayableTarget(provider_id=provider_id, external_id=external_id)
    # Provider-scoped episode wire id — split the prefix and hand the
    # suffix straight to stream(), exactly like /api/stream/{id}.
    target = playable_target(item_id)
    if target is None:
        return None
    return PlayableTarget(provider_id=target[0], external_id=target[1])


def provider_for_item(item_id: str) -> str | None:
    """The provider a playable item id names, or None when it names none.

    The attribution rule the seam and the attestation share: a
    provider-scoped episode wire id names its prefix; a ``gN:`` group key
    names the group's first-seen provider (the one the group's chip strip
    shows first; :func:`resolve_stream` may try the member whose content
    page answered before it, and a member after it may end up serving).
    None for a malformed id or a cold group — the attestation
    then counts the fault under ``play_attestation.UNKNOWN_PROVIDER``
    rather than guessing.
    """
    if not is_group_key(item_id):
        target = playable_target(item_id)
        return target[0] if target is not None else None
    group_key, _ = split_season_key(item_id)
    res = group_resolution(group_key)
    if res is None or not res.sources:
        return None
    return res.sources[0].provider


async def resolve_stream(
    item_id: str,
    translation_id: str | None = None,
    *,
    surface: PlaySurface | None = None,
    raise_errors: bool = False,
) -> StreamResponse | None:
    """The upstream ``StreamResponse`` behind a playable item id, or None.

    Resolves the two playable id families (D2/D3) to their provider, then
    asks :func:`cs_uk_api.play_request.attempt_play` per member — the
    same seam native ``/api/stream/{id}`` crosses, with the same bare
    external ids, ``translation`` (None = default voice, or the picked
    dub id, spec #276) and shared ``httpx`` client. This function owns
    only the id families and the member walk:

      - a movie's ``g2:`` group key → the group's members in first-seen
        order, LED by the member whose content page answered
        (``group_content_member``: content resolution walks the group
        too, so the page may have come from a member other than the
        first; Q47), each called with its BARE external id; if a member
        refuses, the walk continues through the group's other members
        (below). Playability
        is decided on the content's FORM — ``content.form == "movie"`` —
        the same verdict detail renders as ``Type="Movie"``, NOT the
        card's style literal (``SearchResult.type`` can say ``"anime"``
        for an anime FILM; conflating style with form would 404 a film
        the client just opened as a Movie). The shared
        ``resolve_group_content`` also carries the blocklist verdict, so
        a blocked title never gets a stream.
      - an episode wire id (``p1:s1e1``-style) → the provider is the id
        prefix; the episode suffix is handed to ``stream()`` exactly, no
        group-key resolution (episodes are not reverse-lookupable, D2).

    Unplayable ids (a series/season item — a show is not a playable
    thing, the client plays episodes, D3 — a season suffix, a cold group
    key, a blocked title, an unknown provider prefix) yield None. A
    refusal degrades to None → 404, the facade's standing "never 5xx"
    posture (D2). The provider call, translation gate, health recording
    and attestation are delegated to
    :func:`cs_uk_api.play_request.attempt_play`; this resolver owns only
    the id families and the group walk.

    A group key is carried by several providers, so one member's refusal
    is not yet this title's verdict: the walk asks the next member when
    the verdict is walkable (``WALKABLE_VERDICT_CODES`` — a lane fault,
    ``not_found``, or a translation verdict), which is what makes the
    group key a resilient play surface rather than a single point of
    failure. A translation verdict is walkable for the same reason as the
    rest: a dub is per-MEMBER knowledge, so the member whose page listed
    the track the client pinned (spec #276) is a different member from the
    one that lacks it — the walk hands the SAME pinned id to the members
    after it, and the client gets the track it picked rather than a 404
    for a title that plays. Each attempt records its own outcome, so the
    attestation answers "served, by whom, after how many refusals" —
    while the served member's success is the only thing that reaches lane
    health (a walk that ends in refusal has recorded every lane fault on
    the way, ADR-0002 intact). A refusal that is not walkable ends the
    walk: ``gated`` is the client's own verdict about the title, and
    asking the group again is not a repair for it.

    ``surface`` names the route family asking (see ``models.PlaySurface``)
    and is what makes this a *playback* request for
    :mod:`cs_uk_api.play_attestation`: the outcome — served, or refused
    with the verdict that refused it — is recorded under that surface.
    Callers that cross the seam for something other than playback (the
    subtitle route, which resolves to find a track) pass nothing and are
    not counted. A play press can therefore record more than once
    (``playbackinfo`` then ``stream``): the axis counts requests, not
    presses, and the surface tells the two apart.

    ``raise_errors=True`` is the teleport route's mode: the same health
    recordings and the same item-verdict skip, but a refused attempt
    re-raises the provider's typed error so the caller can tell an
    unplayable id (None / item verdict) from "provider unavailable"
    (exception). The teleport route only asks about provider-scoped
    episode ids, so the re-raise happens on the first (and only) member.
    The facade keeps the default None posture.
    """
    if is_group_key(item_id):
        # Series/season keys and cold groups: not playable on their own.
        group_key, season_number = split_season_key(item_id)
        if season_number is not None:
            attest_refusal(provider_for_item(group_key), item_id, "not_found", surface)
            return None
        content = (await resolve_item(group_key)).content
        if content is None or content.form != "movie":
            attest_refusal(provider_for_item(group_key), item_id, "not_found", surface)
            return None
        res = group_resolution(group_key)
        if res is None or not res.sources:
            attest_refusal(None, item_id, "not_found", surface)
            return None
        # The member whose content page answered leads the walk: the page
        # may have been served by a member other than the first (content
        # resolution walks the group too, Q47), and THAT member is the one
        # whose translation ids the client is holding, and the one whose
        # detail page we just proved alive — asking the members after it
        # first would spend an upstream call on a member already known
        # dead. Leading with it is not the same as ENDING on it: when it
        # turns out not to carry the pinned dub, the walk goes on (a track
        # is per-member knowledge), and the members after it are asked with
        # the very id the client picked.
        attempts = member_attempts(
            res.sources, lead=group_content_member(group_key)
        )
        if not attempts:
            # Members exist on record but none can be asked (no usable
            # ids, or every one names a retired provider): the same
            # no-playable-source verdict a cold group gets.
            attest_refusal(None, item_id, "not_found", surface)
            return None
    else:
        # Provider-scoped episode wire id — split the prefix and hand the
        # suffix straight to stream(), exactly like /api/stream/{id}.
        # Episodes are not reverse-lookupable (D2), so an episode has
        # exactly one member and no walk.
        target = playable_target(item_id)
        if target is None:
            attest_refusal(None, item_id, "not_found", surface)
            return None
        attempts = (target,)

    http = get_client()
    for position, (provider_id, external_id) in enumerate(attempts):
        outcome = await attempt_play(
            provider_id,
            external_id,
            translation_id,
            http,
            item_id=item_id,
            surface=surface,
        )
        if outcome.stream is not None:
            if position:
                log.info(
                    "jellyfin playback member fallback id=%s served_by=%s after=%d refusal(s)",
                    item_id,
                    provider_id,
                    position,
                )
            return outcome.stream
        if raise_errors and outcome.error is not None:
            # Teleport's mode (see the docstring): surface the provider's
            # typed error rather than the facade's None → 404.
            raise outcome.error
        if outcome.code is not None and outcome.code not in WALKABLE_VERDICT_CODES:
            # ``gated``: the walk ends here rather than spending another
            # member's upstream call on an answer the group cannot change.
            # A translation verdict does NOT end it — the member that
            # carries the pinned dub may be the next one, which is why
            # those codes are in the walkable set
            # (``_catalog_state.resolution``).
            return None
    return None
