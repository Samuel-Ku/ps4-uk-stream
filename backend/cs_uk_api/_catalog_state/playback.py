"""The playback-translation conversation (deepening: one owner).

The DOMAIN half of the facade's playback routes (folded off the facade
router in #347, moved out of :mod:`resolution` in the deepening wave):
which translations a playable item offers and in what order the
picker's candidates rank, what a dub pick is remembered as, and where a
played episode sits in its season. The wire half — MediaSourceInfo
assembly, the ``item::translation`` source-id codec, HTTP body parsing
— stays in the facade; these accessors hand it typed answers instead of
store lookups.

Import direction: playback -> resolution (the group/content brain) and
._stores (the dub memory) — never the reverse.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..models import Episode, Season, Translation
from ..wire_identity import is_group_key, split_wire_id
from ._stores import dub_for, remember_dub
from .resolution import episode_group_key, resolve_group_content

# ---------------------------------------------------------------------------
#: Multi-source cap (spec #276): at most 8 translations surface as
#: picker candidates — providers with many dubs don't bloat the response.
MAX_TRANSLATION_SOURCES = 8


@dataclass(frozen=True)
class PlaybackEpisodePairing:
    """Where a played episode wire id sits in its series (#347).

    Everything the facade needs to shape the episode's DTO (and its
    next sibling's): the owning group/provider/title plus the matched
    Season/Episode domain models. ``next_episode`` is None on a season
    finale. None answers (no pairing) are unresolvable ids — cold group,
    non-episode id, unknown key.
    """

    group_key: str
    provider_id: str
    series_title: str
    season: Season
    episode: Episode
    next_episode: Episode | None


def _translation_label(
    translations: Sequence[Translation], translation_id: str
) -> str | None:
    """The label for a translation id, or None (spec #276: the picker
    renders labels; the memory stores labels)."""
    for t in translations:
        if t.id == translation_id:
            return t.label
    return None


def ordered_translation_candidates(
    translations: Sequence[Translation],
    *,
    remembered: str | None = None,
    picked_index: int | None = None,
) -> list[Translation]:
    """The picker's candidate order for one PlaybackInfo response
    (spec #276).

    Dedupe by label first (first provider wins), capped at
    ``MAX_TRANSLATION_SOURCES`` during collection; THEN rank: the source
    matching the request's echoed ``picked_index`` (1-based position in
    the deduped list — the switch path) goes first when present,
    otherwise the ``remembered`` dub label goes first so a replay of the
    series defaults to it, otherwise provider order stands. The sort is
    stable, so ties keep the original order.
    """
    deduped: list[Translation] = []
    seen_labels: set[str] = set()
    for t in translations:
        if t.label in seen_labels:
            continue
        seen_labels.add(t.label)
        deduped.append(t)
        if len(deduped) >= MAX_TRANSLATION_SOURCES:
            break

    def rank(t: Translation, idx: int) -> tuple[int, int]:
        # (order group, stable tiebreak): picked/remembered first.
        if picked_index is not None and idx == picked_index:
            return (0, idx)
        if picked_index is None and remembered is not None and t.label == remembered:
            return (0, idx)
        return (1, idx)

    ordered = sorted(enumerate(deduped, start=1), key=lambda pair: rank(pair[1], pair[0]))
    return [t for _, t in ordered]


async def playback_translations(item_id: str) -> tuple[list[Translation], str | None]:
    """(candidate translations, remembered dub label) for a playable item
    (spec #276). The translation list comes from the episode blob (no
    network) or the content page (already fetched); the remembered label
    comes from the user-state dub memory keyed by the SERIES group key.
    Movies are never remembered (v3 decision) — their group key is the
    memory key only for episodes.
    """
    remembered: str | None = None
    if is_group_key(item_id):
        # Movie: content translations; no dub memory.
        content = await resolve_group_content(item_id)
        if content is None:
            return [], None
        return list(content.translations), None

    # Episode wire id: resolve the merged group → the content page → the
    # episode's own translations (fall back to the content's).
    group_key = episode_group_key(item_id)
    if group_key is None:
        return [], None
    content = await resolve_group_content(group_key)
    if content is None:
        return [], None
    # The composite split is wire_identity's grammar; the prefix strip
    # below has no canonical helper and keeps its literal form so the
    # episode matching stays byte-identical (#347).
    provider_id, _ = split_wire_id(content.id)
    prefix = f"{provider_id}:"
    episode_tail = item_id.removeprefix(prefix)
    translations = list(content.translations)
    if content.seasons:
        for season in content.seasons:
            for ep in season.episodes:
                if ep.id == episode_tail or ep.id == item_id:
                    if ep.translations:
                        translations = list(ep.translations)
                    break
    remembered = dub_for(group_key)
    return translations, remembered


async def record_dub_choice(item_id: str, translation_id: str) -> None:
    """Record the viewer's dub pick as per-series memory (spec #276).

    The series group key is resolved from the played item (episode wire
    ids via the reverse lookup; movies are skipped — v3 decision). The
    label is what PlaybackInfo reorders by, so the id is translated
    through the SAME translation list the picker rendered (the episode's
    own dubs, falling back to the content's) before storing. A
    best-effort record: resolution failures just skip the memory.
    """
    if is_group_key(item_id):
        return
    translations, _ = await playback_translations(item_id)
    label = _translation_label(translations, translation_id)
    if label is None:
        return
    group_key = episode_group_key(item_id)
    if group_key is not None:
        remember_dub(group_key, label)


def _played_episode_wire(provider_id: str, episode_id: str) -> str:
    """The provider-scoped wire id an episode DTO carries: the provider
    prefix unless the episode id already embeds it (the same rule the
    facade builds its season-rail ids with — providers are not uniform,
    D2). Kept here because the match must agree with what the client
    plays, while the state package cannot import the facade."""
    prefix = f"{provider_id}:"
    return episode_id if episode_id.startswith(prefix) else prefix + episode_id


async def playback_episode_pair(item_id: str) -> PlaybackEpisodePairing | None:
    """Locate a played episode wire id in its series (#347).

    The ``provider:external`` prefix identifies the merged group
    (reverse lookup, #214), whose season hierarchy holds the episode;
    the next sibling is the same season's following entry. Returns None
    for a non-episode id or an unresolvable group (cold cache / gated
    item) — the caller degrades exactly as before.
    """
    group_key = episode_group_key(item_id)
    if group_key is None:
        return None
    content = await resolve_group_content(group_key)
    if content is None or not content.seasons:
        return None
    provider_id, _ = split_wire_id(content.id)
    for season in content.seasons:
        episodes = season.episodes
        for idx, ep in enumerate(episodes):
            if _played_episode_wire(provider_id, ep.id) != item_id:
                continue
            nxt = episodes[idx + 1] if idx + 1 < len(episodes) else None
            return PlaybackEpisodePairing(
                group_key=group_key,
                provider_id=provider_id,
                series_title=content.title,
                season=season,
                episode=ep,
                next_episode=nxt,
            )
    return None
