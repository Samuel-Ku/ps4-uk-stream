"""Content-based recommendation scorer + row builder (spec #252).

Pure-function tests on the two seams the spec names: the scorer
(weighted cosine — no I/O) and the row builder (rows composed from
profiles/anchors/watched/queries). Never asserts the background
builder's timing or concurrency internals.
"""

from __future__ import annotations

from cs_uk_api.llm import RowIdea, TasteProfile
from cs_uk_api.models import ContentResponse, HomeItem
from cs_uk_api.recommend import (
    LLM_IDEA_ROW_TYPES,
    QUERY_MATCH_BOOST,
    ItemProfile,
    build_recommendation_rows,
    profile_from_content,
    query_boost,
    similarity,
    taste_score,
)

# ------------------------------------------------------------ scorer


def _prof(**kw: object) -> ItemProfile:
    base: dict[str, object] = {
        "genres": frozenset(),
        "people": frozenset(),
        "year": None,
        "form": None,
        "styles": frozenset(),
    }
    base.update(kw)
    return ItemProfile(**base)  # type: ignore[arg-type]


def test_similarity_genres_dominate() -> None:
    """#252: shared genres rank above unrelated ones (weight 1.0)."""
    a = _prof(genres=frozenset({"боєвик", "фантастика"}), year=2021)
    same = _prof(genres=frozenset({"боєвик", "фантастика"}), year=2021)
    other = _prof(genres=frozenset({"драма"}), year=2021)
    assert similarity(a, same) > similarity(a, other)


def test_similarity_form_mismatch_halves() -> None:
    """#252: a form mismatch multiplies the total by 0.5."""
    a = _prof(genres=frozenset({"драма"}), form="movie")
    movie = _prof(genres=frozenset({"драма"}), form="movie")
    series = _prof(genres=frozenset({"драма"}), form="series")
    assert similarity(a, series) == 0.5 * similarity(a, movie)


def test_similarity_year_proximity_only_within_window() -> None:
    """#252: |Δyear| ≤ 2 contributes the 0.3 term; further apart adds 0."""
    a = _prof(genres=frozenset({"боєвик"}), year=2021)
    same_year = _prof(genres=frozenset({"боєвик"}), year=2021)
    near = _prof(genres=frozenset({"боєвик"}), year=2022)
    far = _prof(genres=frozenset({"боєвик"}), year=2018)
    # Within the window the 0.3 year term is present.
    assert similarity(a, near) == similarity(a, same_year) > similarity(a, far)


def test_query_boost_on_title_or_genre() -> None:
    """#252: a query matching the title or a genre adds the fixed boost."""
    p = _prof(genres=frozenset({"фантастика"}))
    assert query_boost(p, "Дюна: Частина друга", ["дюна"]) == QUERY_MATCH_BOOST
    assert query_boost(p, "Чужий", ["фантастика"]) == QUERY_MATCH_BOOST
    assert query_boost(p, "Чужий", ["комедія"]) == 0.0


def test_taste_score_weights_anchors_by_recency() -> None:
    """#252: aggregate taste is the recency-weighted mean over anchors."""
    c = _prof(genres=frozenset({"драма"}), form="movie")
    close = _prof(genres=frozenset({"драма"}), form="movie")
    far = _prof(genres=frozenset({"комедія"}), form="movie")
    s = taste_score(c, [(close, 1.0), (far, 0.7)])
    assert s > taste_score(c, [(far, 1.0), (close, 0.7)])


# ------------------------------------------------------------ rows


def _item(gk: str, title: str) -> HomeItem:
    return HomeItem(group_key=gk, title=title, year=2021, poster=None, form="movie", styles=[], genres=[], providers=["p1"])


def test_recommended_row_ranks_and_excludes_watched() -> None:
    """#252: the row ranks by aggregate taste, excludes watched groups,
    and caps at 20."""
    anchor = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    action = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    drama = _prof(genres=frozenset({"драма"}), form="movie", year=1990)
    rows = build_recommendation_rows(
        home_items=[_item("g2:a", "Екшн"), _item("g2:d", "Драма"), _item("g2:w", "Дивився")],
        profiles={"g2:a": action, "g2:d": drama, "g2:w": action},
        watched={"g2:w"},
        anchors=[(anchor, 1.0)],
        similar_anchor=None,
        queries=[],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.type == "recommended"
    assert row.title == "Рекомендовано для тебе"
    assert [it.group_key for it in row.items] == ["g2:a"]
    # watched excluded even though its profile matches the anchor
    assert all(it.group_key != "g2:w" for it in row.items)


def test_recommended_row_surfaces_from_query_only_signal() -> None:
    """#255: user story 3 — recent searches alone must surface items
    ("things I looked for but didn't watch"). No anchors, only a query
    that matches a candidate: the row appears, ranked by the boost."""
    rows = build_recommendation_rows(
        home_items=[_item("g2:a", "Екшн"), _item("g2:d", "Драма")],
        profiles={
            "g2:a": _prof(genres=frozenset({"боєвик"}), form="movie"),
            "g2:d": _prof(genres=frozenset({"драма"}), form="movie"),
        },
        watched=set(),
        anchors=[],
        similar_anchor=None,
        queries=["боєвик"],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.type == "recommended"
    assert [it.group_key for it in row.items] == ["g2:a"]


def test_recommended_row_omitted_without_signal() -> None:
    """#252: no anchors and no queries → the row is omitted (empty rows
    don't ship)."""
    rows = build_recommendation_rows(
        home_items=[_item("g2:a", "Екшн")],
        profiles={"g2:a": _prof(genres=frozenset({"боєвик"}), form="movie")},
        watched=set(),
        anchors=[],
        similar_anchor=None,
        queries=[],
    )
    assert rows == []


def test_recommended_row_caps_at_20() -> None:
    """#252: at most 20 items in «Рекомендовано для тебе»."""
    anchor = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    prof = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    home_items = [_item(f"g2:{i}", f"Тайтл {i}") for i in range(25)]
    profiles = {f"g2:{i}": prof for i in range(25)}
    rows = build_recommendation_rows(
        home_items=home_items, profiles=profiles, watched=set(),
        anchors=[(anchor, 1.0)], similar_anchor=None, queries=[],
    )
    assert len(rows[0].items) == 20


def test_similar_row_uses_single_anchor_and_caps_at_10() -> None:
    """#252: «Схоже на X» scores against the single most-recent
    in-progress anchor and caps at 10."""
    anchor = _prof(genres=frozenset({"фантастика"}), form="movie", year=2021)
    sf = _prof(genres=frozenset({"фантастика"}), form="movie", year=2021)
    drama = _prof(genres=frozenset({"драма"}), form="movie", year=1990)
    home_items = [_item("g2:sf", "Космос"), _item("g2:d", "Драма")]
    profiles = {"g2:sf": sf, "g2:d": drama}
    rows = build_recommendation_rows(
        home_items=home_items, profiles=profiles, watched=set(),
        anchors=[], similar_anchor=(anchor, "Дюна"), queries=[],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.type == "similar"
    assert row.title == "Схоже на Дюна"
    assert [it.group_key for it in row.items] == ["g2:sf"]


def test_candidates_deduped_across_rows() -> None:
    """#252: the same group surfacing in several home rows ranks once."""
    anchor = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    prof = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    rows = build_recommendation_rows(
        home_items=[_item("g2:a", "Екшн"), _item("g2:a", "Екшн"), _item("g2:b", "Драма")],
        profiles={"g2:a": prof, "g2:b": _prof(genres=frozenset({"драма"}), form="movie")},
        watched=set(),
        anchors=[(anchor, 1.0)],
        similar_anchor=None,
        queries=[],
    )
    assert [it.group_key for it in rows[0].items] == ["g2:a"]


def test_candidates_deduped_by_title_across_group_keys() -> None:
    """#252: the same title under two different group keys (per-row merge
    divergence) ranks once."""
    anchor = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    prof = _prof(genres=frozenset({"боєвик"}), form="movie", year=2021)
    rows = build_recommendation_rows(
        home_items=[_item("g2:a", "Один тайтл"), _item("g2:b", "Один тайтл")],
        profiles={"g2:a": prof, "g2:b": prof},
        watched=set(),
        anchors=[(anchor, 1.0)],
        similar_anchor=None,
        queries=[],
    )
    assert len(rows[0].items) == 1


# --------------------------------------------- LLM enrichment (#293)


def test_genre_weights_rerank_above_unweighted() -> None:
    """#293 AC1: with an active profile a boosted genre ranks its titles
    above the unweighted order."""
    anchor = _prof(genres=frozenset({"боєвик", "драма"}), form="movie")
    action = _prof(genres=frozenset({"боєвик"}), form="movie")
    drama = _prof(genres=frozenset({"драма", "трилер"}), form="movie")
    items = [_item("g2:a", "Екшн"), _item("g2:d", "Драма")]
    profiles = {"g2:a": action, "g2:d": drama}
    kw = {
        "home_items": items,
        "profiles": profiles,
        "watched": set(),
        "anchors": [(anchor, 1.0)],
        "similar_anchor": None,
        "queries": [],
    }
    plain = build_recommendation_rows(**kw)
    weighted = build_recommendation_rows(
        **kw, profile=TasteProfile(genre_weights={"драма": 2.0})
    )
    # Unweighted: action shares 1 of the anchor's 2 genres (cos 1/√2)
    # vs drama's 1/2 — action leads. Weighted: the drama genre term is
    # doubled (2/√4 = 1.0) and flips the order.
    assert [it.group_key for it in plain[0].items] == ["g2:a", "g2:d"]
    assert [it.group_key for it in weighted[0].items] == ["g2:d", "g2:a"]


def test_theme_tags_boost_by_title_or_genre() -> None:
    """#293 AC2: theme tags reuse the query-boost token mechanics — a
    tag matching the title OR a genre label lifts the item above the
    unweighted taste order."""
    anchor = _prof(genres=frozenset({"боєвик", "фантастика"}), form="movie")
    action = _prof(genres=frozenset({"боєвик"}), form="movie")
    drama = _prof(genres=frozenset({"драма"}), form="movie")

    def top(profile: TasteProfile | None) -> list[str]:
        rows = build_recommendation_rows(
            home_items=[_item("g2:a", "Екшн"), _item("g2:d", "Повільна драма")],
            profiles={"g2:a": action, "g2:d": drama},
            watched=set(),
            anchors=[(anchor, 1.0)],
            similar_anchor=None,
            queries=[],
            profile=profile,
        )
        return [it.group_key for it in rows[0].items]

    assert top(None) == ["g2:a"]
    # genre-label match: the tag "драма" IS a genre label — drama
    # (0 taste + 1.0 boost) outranks action (0.707 taste).
    assert top(TasteProfile(theme_tags=("драма",))) == ["g2:d", "g2:a"]
    # title match: the tag "повільна" appears in the title.
    assert top(TasteProfile(theme_tags=("повільна",))) == ["g2:d", "g2:a"]


def test_idea_rows_filter_by_genre_and_cap() -> None:
    """#293 AC3: up to two idea rows appear with ONLY genre-matching
    items, capped at the idea's max, on the fixed llm_idea_N kinds —
    even with no anchor/query taste signal."""
    profile = TasteProfile(
        row_ideas=(
            RowIdea(title="Похмурі драми для тебе", genres=("драма",), max=1),
            RowIdea(title="Екшн для тебе", genres=("боєвик",), max=10),
        )
    )
    rows = build_recommendation_rows(
        home_items=[
            _item("g2:a", "Екшн"),
            _item("g2:d", "Драма"),
            _item("g2:c", "Комедія"),
        ],
        profiles={
            "g2:a": _prof(genres=frozenset({"боєвик"}), form="movie"),
            "g2:d": _prof(genres=frozenset({"драма"}), form="movie"),
            "g2:c": _prof(genres=frozenset({"комедія"}), form="movie"),
        },
        watched=set(),
        anchors=[],
        similar_anchor=None,
        queries=[],
        profile=profile,
    )
    assert [r.type for r in rows] == list(LLM_IDEA_ROW_TYPES)
    assert rows[0].title == "Похмурі драми для тебе"
    assert [it.group_key for it in rows[0].items] == ["g2:d"]
    assert rows[1].title == "Екшн для тебе"
    assert [it.group_key for it in rows[1].items] == ["g2:a"]


def test_idea_row_with_no_matching_items_is_omitted() -> None:
    """#293 user story 6: an idea whose declared genre matches nothing
    in the snapshot produces no row — the curation never lies."""
    profile = TasteProfile(
        row_ideas=(RowIdea(title="Вестерни", genres=("вестерн",), max=5),)
    )
    rows = build_recommendation_rows(
        home_items=[_item("g2:a", "Екшн")],
        profiles={"g2:a": _prof(genres=frozenset({"боєвик"}), form="movie")},
        watched=set(),
        anchors=[],
        similar_anchor=None,
        queries=[],
        profile=profile,
    )
    assert rows == []


def test_no_profile_is_identical_to_empty_profile() -> None:
    """#293 AC4: without an active profile (or with an empty one) the
    rows and ranking are identical to the unweighted behavior — the
    layer is invisible until enabled."""
    anchor = _prof(genres=frozenset({"боєвик", "фантастика"}), form="movie", year=2021)
    items = [_item("g2:a", "Екшн"), _item("g2:b", "Драма"), _item("g2:w", "Дивився")]
    profiles = {
        "g2:a": _prof(genres=frozenset({"боєвик"}), form="movie"),
        "g2:b": _prof(genres=frozenset({"драма"}), form="movie"),
        "g2:w": _prof(genres=frozenset({"боєвик"}), form="movie"),
    }
    kw = {
        "home_items": items,
        "profiles": profiles,
        "watched": {"g2:w"},
        "anchors": [(anchor, 1.0)],
        "similar_anchor": None,
        "queries": ["екшн"],
    }
    assert build_recommendation_rows(**kw) == build_recommendation_rows(
        **kw, profile=TasteProfile()
    )
    # The scorer itself: an empty weight map is the unweighted cosine.
    a = _prof(genres=frozenset({"драма", "трилер"}), form="movie")
    b = _prof(genres=frozenset({"драма"}), form="movie")
    assert similarity(a, b) == similarity(a, b, {})


def test_profile_from_content() -> None:
    """#252: the content-page profile carries genres/people/year/form/
    styles."""
    content = ContentResponse(
        id="p1:dune",
        form="movie",
        title="Дюна",
        year=2021,
        poster=None,
        translations=[{"id": "uk", "label": "Дубляж"}],
        genres=["Фантастика", " Бойовик "],
        people=[{"id": "p:Тімоті", "name": "Тімоті Шаламе"}],
        styles=["anime"],
    )
    p = profile_from_content(content)
    assert p.genres == frozenset({"фантастика", "бойовик"})
    assert p.people == frozenset({"тімоті шаламе"})
    assert p.year == 2021
    assert p.form == "movie"
    assert p.styles == frozenset({"anime"})
