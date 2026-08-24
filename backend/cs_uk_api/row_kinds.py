"""Single declarative table of home-row kinds (spec #323, Row T1 #329).

One ``RowKind`` entry per home-row routing key — the single source of
row-kind facts. The home builder (``home.py``), every facade wire
mapping (``jellyfin/router.py`` + ``jellyfin/dto.py``) and the
deep-rows extension (spec #305) read THIS table instead of their
private vocabularies: a kind's title and item filter, its sources
selector, its wire mappings (Jellyfin Type / CollectionType /
deterministic view id) and its extendability flag all live in one
place. Adding a row kind touches the table only (AC #329) —
``ROW_KINDS`` insertion order IS the canonical home-emission order, and
the derived maps (``VIEW_TYPE_BY_ID``, ``KINDS_BY_JF_TYPE``,
``TYPE_KINDS``) flow from it.

Retired private vocabularies (all replaced by table reads): the home
builder's kind→title / kind→form-filter tuples, the facade's private
view-id / CollectionType / item-Type dicts and reverse index, and the
snapshot's extendable-rows frozenset.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from .models import MediaForm, SearchResult

#: How a row admits listing items (the form filter). ``form`` rows guard
#: against cards whose FORM contradicts the row (upstream mis-filed
#: cards must not surface as junk); ``any`` rows admit everything — the
#: newest/popular/style rows only guard against contradictory FORM on
#: form rows.
FormFilter = Literal["form", "any"]

#: How a row's listings are selected (the sources selector): the
#: provider's ``newest_section`` browse, the animeon ``popular`` browse,
#: the by-type section fan-out keyed by the section's Model B axes
#: (``home.section_row_type``), the playback history groups, or the
#: LLM profile's curated ideas.
SourcesSelector = Literal["newest", "popular", "type", "history", "idea"]


@dataclass(frozen=True)
class RowKind:
    """One declarative row-kind entry (kind → title/filter/sources/wire/flag).

    ``form`` is the Model B form axis a ``form``-filtered row admits
    (movie/series); ``None`` on ``any`` rows (the consistency test pins
    this invariant).
    """

    kind: str
    title: str
    filter: FormFilter
    form: MediaForm | None
    sources: SourcesSelector
    #: Jellyfin item Type for the row's cards. A form-filtered row's
    #: cards render THIS type; an ``any`` row is a mixed aggregate whose
    #: cards render per-item (the facade looks the item's form up in the
    #: table) — the value here keeps the entry complete on the wire
    #: (AC: every entry maps).
    jf_type: str
    collection_type: str
    #: Deep-rows (spec #305): may this row page beyond the snapshot?
    #: The type rows, the form-split «Нещодавно додані» rows and
    #: «Популярні зараз» page; the personalized rows («Нові серії»,
    #: «Нещодавно переглянуто»), the LLM idea rows and the genre rails
    #: stay snapshot-bounded (their pool IS the snapshot). Consumed by
    #: the deep-rows gate (``_catalog_state`` ``extend_row_pool``); the
    #: consistency test pins the split.
    extendable: bool

    @property
    def view_id(self) -> str:
        """Deterministic view id: uuid5 of ``cs-uk-api-view:{kind}``.

        Stable across restarts (a client's cached library list keeps
        working) and reversible — ``VIEW_TYPE_BY_ID`` is the inverse.
        """
        return uuid.uuid5(uuid.NAMESPACE_URL, f"cs-uk-api-view:{self.kind}").hex


#: The one table. Insertion order = the canonical home-emission order
#: (spec #362 D1): the form-split «Нещодавно додані» rows → «Нові
#: серії» → «Нещодавно переглянуто» → «Популярні зараз» → the five type
#: rows (movie, series, anime, cartoon, dorama) → the LLM idea slots.
#: ``build_home_rows`` emits each kind's segment at its table position,
#: omitting empty/unsignalled rows (the recent rows, «Нові серії» and
#: «Нещодавно переглянуто» are conditional — omission, not reordering).
#: The retired «Новинки» (``newest``) is NOT a row kind (retired
#: 2026-08-14, spec #263); the personalized «Рекомендовано для тебе» /
#: «Схоже на X» rows and the ``genre:<slug>`` rails stay outside the
#: table by design (recipe-inserted / parameterized kinds are not
#: enumerable — spec #362 D1).
ROW_KINDS: dict[str, RowKind] = {
    "recent_movie": RowKind(
        kind="recent_movie",
        title="Нещодавно додані: Фільми",
        filter="form",
        form="movie",
        sources="newest",
        jf_type="Movie",
        collection_type="movies",
        extendable=True,
    ),
    "recent_series": RowKind(
        kind="recent_series",
        title="Нещодавно додані: Серіали",
        filter="form",
        form="series",
        sources="newest",
        jf_type="Series",
        collection_type="tvshows",
        extendable=True,
    ),
    "new_episodes": RowKind(
        kind="new_episodes",
        title="Нові серії",
        filter="form",
        form="series",
        sources="newest",
        jf_type="Series",
        collection_type="tvshows",
        extendable=False,
    ),
    "recently_watched": RowKind(
        kind="recently_watched",
        title="Нещодавно переглянуто",
        filter="any",
        form=None,
        sources="history",
        jf_type="Series",
        collection_type="tvshows",
        extendable=False,
    ),
    "popular": RowKind(
        kind="popular",
        title="Популярні зараз",
        filter="any",
        form=None,
        sources="popular",
        jf_type="Series",
        collection_type="tvshows",
        extendable=True,
    ),
    "movie": RowKind(
        kind="movie",
        title="Фільми",
        filter="form",
        form="movie",
        sources="type",
        jf_type="Movie",
        collection_type="movies",
        extendable=True,
    ),
    "series": RowKind(
        kind="series",
        title="Серіали",
        filter="form",
        form="series",
        sources="type",
        jf_type="Series",
        collection_type="tvshows",
        extendable=True,
    ),
    "anime": RowKind(
        kind="anime",
        title="Аніме",
        filter="any",
        form=None,
        sources="type",
        jf_type="Series",
        collection_type="tvshows",
        extendable=True,
    ),
    "cartoon": RowKind(
        kind="cartoon",
        title="Мультфільми",
        filter="any",
        form=None,
        sources="type",
        jf_type="Series",
        collection_type="tvshows",
        extendable=True,
    ),
    "dorama": RowKind(
        kind="dorama",
        title="Дорами",
        filter="any",
        form=None,
        sources="type",
        jf_type="Series",
        collection_type="tvshows",
        extendable=True,
    ),
    # Spec #290: the LLM-proposed idea rows use fixed slots so their
    # view ids stay stable across profile refreshes. The ``title`` is a
    # never-rendered placeholder — the profile idea supplies the real
    # label (CONTEXT.md «LLM taste profile»: the curation never lies).
    "llm_idea_1": RowKind(
        kind="llm_idea_1",
        title="Ідея",
        filter="any",
        form=None,
        sources="idea",
        jf_type="Series",
        collection_type="tvshows",
        extendable=False,
    ),
    "llm_idea_2": RowKind(
        kind="llm_idea_2",
        title="Ідея",
        filter="any",
        form=None,
        sources="idea",
        jf_type="Series",
        collection_type="tvshows",
        extendable=False,
    ),
}

#: Reverse view-id map — the inverse of ``RowKind.view_id``. The facade
#: translates a client's ``parentId`` back to a row kind.
VIEW_TYPE_BY_ID: dict[str, str] = {rk.view_id: rk.kind for rk in ROW_KINDS.values()}

#: Reverse Jellyfin-Type map: wire Type → the row kinds that render as
#: it. Built from the WHOLE table — the mixed-aggregate rows (popular,
#: recently watched, LLM ideas) are Series-typed entries too. Consumers
#: translate ``includeItemTypes`` back to kinds and then check
#: per-item form, so the extra kinds never change the wire filter (an
#: item's form is always movie/series).
KINDS_BY_JF_TYPE: dict[str, frozenset[str]] = {
    t: frozenset(rk.kind for rk in ROW_KINDS.values() if rk.jf_type == t)
    for t in {rk.jf_type for rk in ROW_KINDS.values()}
}

#: The five type rows, in home order (the ``sources == "type"`` kinds in
#: table order) — the by-type fan-out the home builder iterates.
TYPE_KINDS: tuple[str, ...] = tuple(
    rk.kind for rk in ROW_KINDS.values() if rk.sources == "type"
)


def item_matches_row(kind: str, item: SearchResult) -> bool:
    """True when a listing item belongs in the row of the given kind.

    Reads the table's form filter: a ``form`` row admits only items
    whose ``form`` equals the row's axis (guards against upstream
    mis-filed cards — a series whose bare URL an adapter classified as
    a film must not leak into the movie row); ``any`` rows admit
    everything (an item without the style tag is still legitimate
    content from a style section).
    """
    entry = ROW_KINDS[kind]
    if entry.filter == "form":
        return item.form == entry.form
    return True


__all__ = [
    "KINDS_BY_JF_TYPE",
    "ROW_KINDS",
    "TYPE_KINDS",
    "VIEW_TYPE_BY_ID",
    "RowKind",
    "item_matches_row",
]
