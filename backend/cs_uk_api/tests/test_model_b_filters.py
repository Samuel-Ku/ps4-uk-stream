"""Model B filter axes on /api/search and /api/browse (ADR-0001, ticket #134).

Seams under test:

  - ``GET /api/search?form=movie|series`` — exact-or-None filter: an
    item passes iff ``item.form == form``; absent = any (existing
    behaviour unchanged).

  - ``GET /api/search?style=anime|cartoon|dorama[,anime,...]`` —
    comma-separated intersection: an item passes iff it carries at
    least one requested style; absent = any. Invalid tokens → 400
    ``invalid_style``. There is deliberately NO ordinary-only token on
    search (CONTEXT.md «Search filter axes»): ``?style`` is a plain
    intersection list.

  - The filter runs BEFORE the merge, so a filtered search never forms
    a group from a non-matching member.

  - The ``/api/search`` cache key carries both axes (ADR-0001
    obligation): filtered and unfiltered searches for the same ``q``
    never share an entry, and two different filters never share either.

  - ``/api/browse`` section filtering honours the section's
    ``form``/``styles`` match semantics from CONTEXT.md (3-case
    styles: None passes anything, ∅ passes ordinary-only, non-empty
    passes on intersection). Undeclared axes (both None) pass
    everything, so un-migrated sections behave unchanged.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import respx
from fastapi.testclient import TestClient

from cs_uk_api._catalog_state import search_cache
from cs_uk_api.main import app
from cs_uk_api.models import SearchResult, Section
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.providers.yts import YtsProvider


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS + search cache so filter tests don't
    leak real upstream calls or cached entries into assertions."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    search_cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        search_cache.clear()


def _result(
    pid: str,
    title: str,
    *,
    form: str | None,
    styles: set[str],
    n: str = "1",
) -> SearchResult:
    # Contract #135: the legacy ``type`` axis is gone — ``form`` is the
    # merge key and ``styles`` the tag set; nothing else to decouple.
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        title=title,
        poster=f"https://{pid}.example/{n}.jpg",
        url=f"https://{pid}.example/{n}",
        form=cast(Any, form),
        styles=frozenset(cast(Any, styles)),
    )


class _StubBase(BaseProvider):
    """Minimal search+browse stub; content()/stream() are unused here."""

    types = ("movie", "series")

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _search_stub(pid: str, results: list[SearchResult]) -> BaseProvider:
    class _Stub(_StubBase):
        def __init__(self) -> None:
            self.id = pid
            self.name = pid.title()
            self._results = list(results)
            self.calls = 0

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            self.calls += 1
            return list(self._results)

    return _Stub()


def _browse_stub(
    pid: str,
    sections: tuple[Section, ...],
    results: dict[str, list[SearchResult]],
) -> BaseProvider:
    class _Stub(_StubBase):
        def __init__(self) -> None:
            self.id = pid
            self.name = pid.title()
            self.sections = sections
            self._results = results
            self.calls = 0

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return []

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            self.calls += 1
            items = self._results.get(section, [])
            return list(items), False

    return _Stub()


# ---------------------------------------------------------------------------
# /api/search: form filter
# ---------------------------------------------------------------------------


def test_search_form_filter_narrows_to_matching_form() -> None:
    movie = _result("p1", "Дюна", form="movie", styles=set())
    series = _result("p1", "Дюна: Сериал", form="series", styles=set(), n="2")
    PROVIDERS["p1"] = _search_stub("p1", [movie, series])

    r = TestClient(app).get("/api/search?q=дюна&form=series")
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["form"] == "series"
    assert groups[0]["title"] == "Дюна: Сериал"


def test_search_form_absent_returns_all() -> None:
    movie = _result("p1", "Дюна", form="movie", styles=set())
    series = _result("p1", "Дюна: Сериал", form="series", styles=set(), n="2")
    PROVIDERS["p1"] = _search_stub("p1", [movie, series])

    r = TestClient(app).get("/api/search?q=дюна")
    assert r.status_code == 200
    assert len(r.json()["groups"]) == 2


# ---------------------------------------------------------------------------
# /api/search: style filter
# ---------------------------------------------------------------------------


def test_search_style_filter_intersection() -> None:
    anime = _result("p1", "Наруто", form="series", styles={"anime"})
    ordinary = _result("p1", "Слово Пацана", form="series", styles=set(), n="2")
    dorama = _result("p1", "К-дорама", form="series", styles={"dorama"}, n="3")
    PROVIDERS["p1"] = _search_stub("p1", [anime, ordinary, dorama])

    r = TestClient(app).get("/api/search?q=x&style=anime")
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert [g["title"] for g in groups] == ["Наруто"]


def test_search_style_multi_token_intersection() -> None:
    anime = _result("p1", "Наруто", form="series", styles={"anime"})
    dorama = _result("p1", "К-дорама", form="series", styles={"dorama"}, n="2")
    both = _result("p1", "Аніме-дорама", form="series", styles={"anime", "dorama"}, n="3")
    PROVIDERS["p1"] = _search_stub("p1", [anime, dorama, both])

    r = TestClient(app).get("/api/search?q=x&style=anime,dorama")
    assert r.status_code == 200
    # Intersection: an item passes iff it carries at least one of the
    # requested styles — so all three match, including the single-tag ones.
    assert len(r.json()["groups"]) == 3


def test_search_style_absent_returns_all() -> None:
    anime = _result("p1", "Наруто", form="series", styles={"anime"})
    ordinary = _result("p1", "Слово Пацана", form="series", styles=set(), n="2")
    PROVIDERS["p1"] = _search_stub("p1", [anime, ordinary])

    r = TestClient(app).get("/api/search?q=x")
    assert r.status_code == 200
    assert len(r.json()["groups"]) == 2


def test_search_invalid_style_token_returns_400() -> None:
    PROVIDERS["p1"] = _search_stub("p1", [])
    r = TestClient(app).get("/api/search?q=x&style=mecha")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_style"


def test_search_invalid_form_token_returns_400() -> None:
    """Ticket #141: a form typo gets the same custom envelope as a style
    typo — 400 ``invalid_form``, not FastAPI's default 422 for the old
    ``Literal`` param. The client parses both axes identically."""
    PROVIDERS["p1"] = _search_stub("p1", [])
    r = TestClient(app).get("/api/search?q=x&form=mecha")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_form"


def test_search_empty_form_token_behaves_like_absent() -> None:
    """Empty/blank ``?form=`` = absent = any (mirrors the style axis)."""
    movie = _result("p1", "Дюна", form="movie", styles=set())
    series = _result("p1", "Серіал", form="series", styles=set(), n="2")
    PROVIDERS["p1"] = _search_stub("p1", [movie, series])

    r = TestClient(app).get("/api/search?q=x&form=")
    assert r.status_code == 200
    assert len(r.json()["groups"]) == 2


def test_search_style_no_ordinary_only_token() -> None:
    """There is no way to request ordinary-only via ?style= (CONTEXT.md):
    an empty value behaves like absent, and there is no magic token."""
    ordinary = _result("p1", "Слово Пацана", form="series", styles=set())
    anime = _result("p1", "Наруто", form="series", styles={"anime"}, n="2")
    PROVIDERS["p1"] = _search_stub("p1", [ordinary, anime])

    # Empty style value = absent = any.
    r = TestClient(app).get("/api/search?q=x&style=")
    assert r.status_code == 200
    assert len(r.json()["groups"]) == 2


# ---------------------------------------------------------------------------
# Filter runs before the merge: a filtered search never forms a group
# from a non-matching member
# ---------------------------------------------------------------------------


def test_search_filter_applies_before_merge() -> None:
    # Both providers return "Дюна" as the SAME type (series) with a
    # year-soft match, so WITHOUT the filter they'd merge into one group.
    # With ?form=series the movie-form member must be dropped BEFORE the
    # merge: if the filter ran after, the group would form first (its
    # canonical form is the first-seen movie member) and be filtered out
    # wholesale — yielding zero groups instead of one series-only group.
    movie = _result("p1", "Дюна", form="movie", styles=set())
    series = _result("p2", "Дюна", form="series", styles=set())
    PROVIDERS["p1"] = _search_stub("p1", [movie])
    PROVIDERS["p2"] = _search_stub("p2", [series])

    r = TestClient(app).get("/api/search?q=дюна&form=series")
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["form"] == "series"
    assert [s["provider"] for s in groups[0]["sources"]] == ["p2"]


# ---------------------------------------------------------------------------
# Cache-key separation (ADR-0001 obligation)
# ---------------------------------------------------------------------------


def test_search_cache_key_carries_axes() -> None:
    """Filtered and unfiltered searches for the same q must not share a
    cache entry — the provider is re-invoked per distinct axis tuple."""
    hits = [
        _result("p1", "Дюна", form="movie", styles=set()),
        _result("p1", "Наруто", form="series", styles={"anime"}, n="2"),
    ]
    stub = _search_stub("p1", hits)
    PROVIDERS["p1"] = stub
    client = TestClient(app)

    client.get("/api/search?q=x")
    client.get("/api/search?q=x")  # cache hit
    assert stub.calls == 1

    client.get("/api/search?q=x&form=movie")
    assert stub.calls == 2  # different axis tuple → new entry

    client.get("/api/search?q=x&form=movie")  # cache hit
    assert stub.calls == 2

    client.get("/api/search?q=x&style=anime")
    assert stub.calls == 3

    client.get("/api/search?q=x&form=movie&style=anime")
    assert stub.calls == 4

    # Distinct style filters never share an entry either.
    client.get("/api/search?q=x&style=anime,dorama")
    assert stub.calls == 5


# ---------------------------------------------------------------------------
# /api/browse: section form/styles match semantics
# ---------------------------------------------------------------------------


def test_browse_section_form_filter() -> None:
    movie = _result("p1", "Аніме-фільм", form="movie", styles={"anime"})
    series = _result("p1", "Аніме-серіал", form="series", styles={"anime"}, n="2")
    sec = Section(
        id="films",
        title="Фільми",
        form=cast(Any, "movie"),
    )
    PROVIDERS["p1"] = _browse_stub("p1", (sec,), {"films": [movie, series]})

    r = TestClient(app).get("/api/browse?provider=p1&section=films")
    assert r.status_code == 200
    titles = [item["title"] for item in r.json()["results"]]
    assert titles == ["Аніме-фільм"]


def test_browse_section_styles_intersection() -> None:
    anime = _result("p1", "Наруто", form="series", styles={"anime"})
    ordinary = _result("p1", "Слово Пацана", form="series", styles=set(), n="2")
    sec = Section(
        id="ani",
        title="Аніме",
        styles=frozenset(cast(Any, {"anime"})),
    )
    PROVIDERS["p1"] = _browse_stub("p1", (sec,), {"ani": [anime, ordinary]})

    r = TestClient(app).get("/api/browse?provider=p1&section=ani")
    assert r.status_code == 200
    titles = [item["title"] for item in r.json()["results"]]
    assert titles == ["Наруто"]


def test_browse_section_styles_empty_is_ordinary_only() -> None:
    """∅ styles on a section passes only ordinary-only items (CONTEXT.md
    3-case rule) — the one place ordinary-only is expressible."""
    ordinary = _result("p1", "Слово Пацана", form="series", styles=set())
    anime = _result("p1", "Наруто", form="series", styles={"anime"}, n="2")
    sec = Section(
        id="ord",
        title="Звичайне",
        styles=frozenset(),
    )
    PROVIDERS["p1"] = _browse_stub("p1", (sec,), {"ord": [ordinary, anime]})

    r = TestClient(app).get("/api/browse?provider=p1&section=ord")
    assert r.status_code == 200
    titles = [item["title"] for item in r.json()["results"]]
    assert titles == ["Слово Пацана"]


def test_browse_section_no_axes_passes_everything() -> None:
    """Sections that haven't declared form/styles (both None) pass
    everything — un-migrated sections behave unchanged."""
    movie = _result("p1", "Дюна", form="movie", styles=set())
    anime = _result("p1", "Наруто", form="series", styles={"anime"}, n="2")
    sec = Section(id="all", title="Все")
    PROVIDERS["p1"] = _browse_stub("p1", (sec,), {"all": [movie, anime]})

    r = TestClient(app).get("/api/browse?provider=p1&section=all")
    assert r.status_code == 200
    assert len(r.json()["results"]) == 2


# ---------------------------------------------------------------------------
# English lane (YTS, spec #374, ticket #380): the filter axes treat
# original-English items identically — plain movie form, ∅ styles
# ---------------------------------------------------------------------------

_YTS_FIX = Path(__file__).parent / "fixtures" / "yts"
_YTS_LIST_URL = re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")


def _register_yts() -> None:
    """Isolated registry holding ONLY the real YtsProvider (fixture-
    mocked upstream; no other provider can leak a network call)."""
    PROVIDERS.clear()
    PROVIDERS["yts"] = YtsProvider()


def test_browse_section_filter_admits_english_movies_like_any_movie_section() -> None:
    """Ticket #380 AC2 (browse leg): the YTS «movies» section declares
    ``form=movie`` (+ styles None pass-any), so the Model B section
    filter runs over its English cards exactly as over any provider's —
    every plain movie passes unchanged."""
    _register_yts()
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_YTS_LIST_URL).respond(
            200, text=(_YTS_FIX / "newest_page1.json").read_text(encoding="utf-8")
        )
        r = TestClient(app).get("/api/browse?provider=yts&section=movies&page=1")
    assert r.status_code == 200
    results = r.json()["results"]
    assert [it["id"] for it in results] == ["yts:tt33050528", "yts:tt29334102"]
    assert all(it["form"] == "movie" for it in results)
    assert all(it["styles"] == [] for it in results)


def test_search_form_axis_treats_english_items_identically() -> None:
    """Ticket #380 AC2 (search leg): ``?form=movie`` keeps the English
    hits and ``?form=series`` drops them — the same exact-or-None axis
    every provider's results ride; no special-casing either way."""
    _register_yts()
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_YTS_LIST_URL).respond(
            200, text=(_YTS_FIX / "search_dune.json").read_text(encoding="utf-8")
        )
        client = TestClient(app)
        unfiltered = client.get("/api/search?q=dune")
        as_movie = client.get("/api/search?q=dune&form=movie")
        as_series = client.get("/api/search?q=dune&form=series")
    assert unfiltered.status_code == as_movie.status_code == as_series.status_code == 200
    unfiltered_ids = [g["group_key"] for g in unfiltered.json()["groups"]]
    assert len(unfiltered_ids) == 2
    assert [g["group_key"] for g in as_movie.json()["groups"]] == unfiltered_ids
    assert all(
        source["provider"] == "yts"
        for group in as_movie.json()["groups"]
        for source in group["sources"]
    )
    assert all(group["form"] == "movie" for group in as_movie.json()["groups"])
    assert as_series.json()["groups"] == []
