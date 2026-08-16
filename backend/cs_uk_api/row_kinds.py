"""Single declarative table of home-row kinds (spec #323, Row T1 #329).

One ``RowKind`` entry per home-row routing key — the single source of
row-kind facts. The home builder (``home.py``), the facade view maps
(``jellyfin/router.py``) and the deep-rows extension (spec #305 — not
yet in this tree) read THIS table instead of their private
vocabularies: a kind's title and item filter, its sources selector,
its wire mappings (Jellyfin Type / CollectionType / deterministic view
id) and its extendability flag all live in one place. Adding a row kind
touches the table only (AC #329) — ``ROW_KINDS`` insertion order IS the
home order, and the derived maps (``VIEW_TYPE_BY_ID``,
``KINDS_BY_JF_TYPE``, ``TYPE_KINDS``) flow from it.

The retired private vocabularies: ``home._TYPE_ORDER`` (kind → title),
``home._item_matches_row`` (kind → form filter),
``router._VIEW_ID_BY_TYPE``/``_COLLECTION_TYPE_BY_ROW``/``_JF_TYPE_BY_ROW``
and the ``_HOME_KINDS_BY_JF_TYPE`` reverse index.
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
#: or the by-type section fan-out keyed by the section's Model B axes
#: (``home.section_row_type``).
SourcesSelector = Literal["newest", "popular", "type"]


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
    #: Personalized rows (newest/popular) stay snapshot-bounded; the
    #: type rows are the ones the extension pages. Consumed by the
    #: deep-rows extension when it lands; the consistency test pins the
    #: split.
    extendable: bool

    @property
    def view_id(self) -> str:
        """Deterministic view id: uuid5 of ``cs-uk-api-view:{kind}``.

        Stable across restarts (a client's cached library list keeps
        working) and reversible — ``VIEW_TYPE_BY_ID`` is the inverse.
        """
        return uuid.uuid5(uuid.NAMESPACE_URL, f"cs-uk-api-view:{self.kind}").hex


#: The one table. Insertion order = the spec's home order (v3 spec
#: §3.1): «Новинки» → «Популярні зараз» → the five type rows (movie,
#: series, anime, cartoon, dorama).
ROW_KINDS: dict[str, RowKind] = {
    "newest": RowKind(
        kind="newest",
        title="Новинки",
        filter="any",
        form=None,
        sources="newest",
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
        extendable=False,
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
}

#: Reverse view-id map — the inverse of ``RowKind.view_id``. The facade
#: translates a client's ``parentId`` back to a row kind.
VIEW_TYPE_BY_ID: dict[str, str] = {rk.view_id: rk.kind for rk in ROW_KINDS.values()}

#: Reverse Jellyfin-Type map: wire Type → the row kinds that render as
#: it. Built from the WHOLE table — newest/popular are Series-aggregate
#: rows too. Consumers translate ``includeItemTypes`` back to kinds and
#: then check per-item form, so the extra kinds never change the wire
#: filter (an item's form is always movie/series).
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
