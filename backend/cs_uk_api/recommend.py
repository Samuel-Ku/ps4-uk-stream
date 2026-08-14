"""Content-based recommendation rows (spec #252) — pure functions.

Two personalized home rows, «Рекомендовано для тебе» and «Схоже на X»,
rank the home snapshot's groups by similarity to the viewer's taste:
content profiles ({genres, people, year, form, styles}) built from the
providers' content pages, scored by a weighted cosine, with recent
search queries adding a fixed boost and already-watched items excluded.

This module is deliberately pure — no I/O, no provider access — so the
scorer and the row composition are unit-testable and the scorer can be
replaced later (an LLM ranker) without touching the rows (spec #252
§LLM layer: the scorer is the pluggable seam).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from .models import ContentResponse, HomeItem, HomeRow

#: «Рекомендовано для тебе» row cap (spec #252).
RECOMMENDED_LIMIT = 20
#: «Схоже на X» row cap (spec #252).
SIMILAR_LIMIT = 10
#: Up to 3 most recent watched items anchor the taste profile.
MAX_ANCHORS = 3

#: Spec §Scoring weights: genres 1.0, people 0.9, styles 0.4, year
#: proximity 0.3 (|Δyear| ≤ 2, else 0); a form mismatch multiplies the
#: total by 0.5.
_GENRE_W = 1.0
_PEOPLE_W = 0.9
_STYLE_W = 0.4
_YEAR_W = 0.3
_YEAR_WINDOW = 2
_FORM_MISMATCH = 0.5

#: Fixed boost added once per query whose tokens match the title or a
#: genre (spec §Scoring "Query matches ... add a fixed boost").
QUERY_MATCH_BOOST = 1.0

#: Recency weights for the up-to-3 taste anchors, newest first.
ANCHOR_WEIGHTS = (1.0, 0.7, 0.5)

#: Row kinds the recommendation builder emits.
RECOMMENDED_ROW_TYPE = "recommended"
SIMILAR_ROW_TYPE = "similar"


@dataclass(frozen=True)
class ItemProfile:
    """Content-page taste profile of one group (spec #252 §Item profiles)."""

    genres: frozenset[str]
    people: frozenset[str]
    year: int | None
    form: str | None
    styles: frozenset[str]


def profile_from_content(content: ContentResponse) -> ItemProfile:
    """Project a content page onto the taste profile.

    Tokens are normalized to lower-case for stable set matching; the
    original labels stay on the wire surfaces (genres, People).
    """
    return ItemProfile(
        genres=frozenset(g.strip().lower() for g in content.genres if g.strip()),
        people=frozenset(p.name.strip().lower() for p in content.people if p.name.strip()),
        year=content.year,
        form=content.form,
        styles=frozenset(s for s in (content.styles or frozenset())),
    )


def _cosine(a: AbstractSet[str], b: AbstractSet[str]) -> float:
    """Cosine over two feature sets (0 when either side is empty)."""
    if not a or not b:
        return 0.0
    inter = len(set(a) & set(b))
    return inter / math.sqrt(len(a) * len(b))


def _year_proximity(a: int | None, b: int | None) -> float:
    if a is None or b is None:
        return 0.0
    return 1.0 if abs(a - b) <= _YEAR_WINDOW else 0.0


def similarity(a: ItemProfile, b: ItemProfile) -> float:
    """Weighted content similarity between two profiles (spec §Scoring).

    Sum of the weighted cosine terms over genres/people/styles plus the
    year-proximity term; a form mismatch (movie vs series) halves the
    total so movies mostly recommend movies and series mostly series.
    """
    score = (
        _GENRE_W * _cosine(a.genres, b.genres)
        + _PEOPLE_W * _cosine(a.people, b.people)
        + _STYLE_W * _cosine(a.styles, b.styles)
        + _YEAR_W * _year_proximity(a.year, b.year)
    )
    if a.form is not None and b.form is not None and a.form != b.form:
        score *= _FORM_MISMATCH
    return score


def query_boost(profile: ItemProfile, title: str, queries: Sequence[str]) -> float:
    """Fixed boost per query whose tokens match the title or a genre.

    Case-insensitive substring match on the title; genre tokens match as
    substrings of the lower-cased genre labels.
    """
    title_l = title.lower()
    boost = 0.0
    for q in queries:
        token = q.strip().lower()
        if not token:
            continue
        if token in title_l or any(token in g for g in profile.genres):
            boost += QUERY_MATCH_BOOST
    return boost


def taste_score(candidate: ItemProfile, anchors: Sequence[tuple[ItemProfile, float]]) -> float:
    """Aggregate taste: the recency-weighted mean of per-anchor similarities."""
    total_w = sum(w for _, w in anchors)
    if not anchors or total_w <= 0:
        return 0.0
    return sum(similarity(candidate, anchor) * w for anchor, w in anchors) / total_w


__all__ = [
    "ANCHOR_WEIGHTS",
    "MAX_ANCHORS",
    "QUERY_MATCH_BOOST",
    "RECOMMENDED_LIMIT",
    "RECOMMENDED_ROW_TYPE",
    "SIMILAR_LIMIT",
    "SIMILAR_ROW_TYPE",
    "ItemProfile",
    "build_recommendation_rows",
    "profile_from_content",
    "query_boost",
    "similarity",
    "taste_score",
]


def build_recommendation_rows(
    *,
    home_items: Sequence[HomeItem],
    profiles: Mapping[str, ItemProfile],
    watched: AbstractSet[str],
    anchors: Sequence[tuple[ItemProfile, float]],
    similar_anchor: tuple[ItemProfile, str] | None,
    queries: Sequence[str],
) -> list[HomeRow]:
    """«Рекомендовано для тебе» + «Схоже на X», or [] when no signal.

    Candidates are the home-snapshot groups that have a profile and are
    not in ``watched``. Both rows are omitted when they have no signal
    (no anchors/queries, or nothing scores above zero) — the existing
    home rule: empty rows don't ship. ``anchors`` are (profile, recency
    weight) pairs, newest first; ``similar_anchor`` is the single most
    recent in-progress (profile, title) for «Схоже на X».
    """
    # A group can surface in more than one home row (e.g. «Новинки» and
    # a type row) — and the same title can carry DIFFERENT group keys
    # across rows (the snapshot's merge can diverge per row). Candidates
    # are deduped by group key first, then by (title, year), first-seen
    # order preserved, so a title never ranks twice.
    seen_keys: set[str] = set()
    seen_titles: set[tuple[str, int | None]] = set()
    pool: list[tuple[HomeItem, ItemProfile]] = []
    for item in home_items:
        if item.group_key in seen_keys:
            continue
        seen_keys.add(item.group_key)
        title_key = (item.title.strip().lower(), item.year)
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        if item.group_key in profiles and item.group_key not in watched:
            pool.append((item, profiles[item.group_key]))
    rows: list[HomeRow] = []

    # Any signal — watched-anchor taste OR a recent search query — can
    # surface the row (spec #252 user story 3: "things I looked for but
    # didn't watch also surface"). With no anchors, taste_score is 0 and
    # only query-matching candidates score above zero.
    if (anchors or queries) and pool:
        scored = [
            (item, taste_score(prof, anchors) + query_boost(prof, item.title, queries))
            for item, prof in pool
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top = [item for item, score in scored if score > 0][:RECOMMENDED_LIMIT]
        if top:
            rows.append(
                HomeRow(
                    title="Рекомендовано для тебе",
                    type=RECOMMENDED_ROW_TYPE,
                    items=top,
                )
            )

    if similar_anchor is not None and pool:
        anchor_prof, anchor_title = similar_anchor
        scored = [(item, similarity(prof, anchor_prof)) for item, prof in pool]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top = [item for item, score in scored if score > 0][:SIMILAR_LIMIT]
        if top:
            rows.append(
                HomeRow(
                    title=f"Схоже на {anchor_title}",
                    type=SIMILAR_ROW_TYPE,
                    items=top,
                )
            )

    return rows
