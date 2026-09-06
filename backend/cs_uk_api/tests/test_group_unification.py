"""Cross-language IMDb-keyed group unification (spec #395).

The ``g3:`` identity tier: listing items carrying a provider-asserted
IMDb id merge by tt-number ACROSS languages (Дюна + Dune = one card);
items without an id keep the ``g2:`` alias+form+year tier, unchanged.
The two namespaces coexist; nothing mutates old keys.

Pinned here:
  - identity: same tt merges, different tt never merges, idless items
    stay g2-only, and a g3 group carries BOTH namespaces in member_keys;
  - grammar: ``is_group_key`` accepts both prefixes, ``parse_playable_id``
    is UNCHANGED on every existing id shape (standing parity gate);
  - state: a resume written against the g3 card persists; a pre-existing
    g2-keyed entry does NOT surface on the g3 card (the documented loss);
  - alias: the g3 card prefers a cyrillic-locale member's title;
  - wire: a g3 key round-trips through group resolution and the facade.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from cs_uk_api.merge import merge_results
from cs_uk_api.models import SearchResult
from cs_uk_api.wire_identity import (
    GROUP_KEY_PREFIX,
    GROUP_KEY_PREFIX_G3,
    group_key_from_imdb,
    is_group_key,
    item_group_key,
    parse_playable_id,
)


def item(
    pid: str,
    title: str,
    media_type: str = "movie",
    year: int | None = None,
    n: str = "1",
    imdb: str | None = None,
) -> SearchResult:
    extra: dict[str, Any] = {"imdb_id": imdb} if imdb else {}
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=cast(Any, media_type),
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
        **extra,
    )


# ---------------------------------------------------------------------------
# Identity tier
# ---------------------------------------------------------------------------


def test_same_imdb_merges_across_languages() -> None:
    """«Дюна / Dune (2021)» (uk, idless) + «Dune» (en, yts, tt-asserted)
    = ONE g3 group, both sources — the realistic listing bridge."""
    groups = merge_results([
        item("uakino", "Дюна / Dune (2021)", year=2021, n="6268"),
        item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814"),
    ])
    assert len(groups) == 1
    g = groups[0]
    assert g.key.startswith("g3:")
    assert {s.provider for s in g.sources} == {"uakino", "yts"}


def test_same_imdb_merges_even_with_different_titles_and_years() -> None:
    """tt equality is the merge rule — year/title disagreements are data
    noise (logged), not merge blockers."""
    groups = merge_results([
        item("p1", "Чотири кімнати / Four Rooms", year=1995, n="a", imdb="tt0110601"),
        item("yts", "Four Rooms", year=1996, n="tt0110601", imdb="tt0110601"),
    ])
    assert len(groups) == 1
    assert groups[0].key.startswith("g3:")


def test_different_imdb_never_merges() -> None:
    groups = merge_results([
        item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814"),
        item("yts", "Dune Part Two", year=2024, n="tt15239678", imdb="tt15239678"),
    ])
    assert len(groups) == 2


def test_same_title_different_imdb_stays_apart() -> None:
    """Remakes with identical aliases but different tt-numbers do NOT
    collapse — the id wins over the title."""
    groups = merge_results([
        item("p1", "Dune", year=1984, n="tt0087299", imdb="tt0087299"),
        item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814"),
    ])
    assert len(groups) == 2


def test_idless_items_keep_g2_only() -> None:
    """Providers without ids are untouched: pure-g2 groups keep g2 keys."""
    groups = merge_results([
        item("uakino", "Дюна", year=2021, n="6268"),
        item("eneyida", "Дюна", year=2021, n="x2"),
    ])
    assert len(groups) == 1
    assert not groups[0].key.startswith("g3:")
    assert is_group_key(groups[0].key)


def test_mixed_group_registers_both_namespaces() -> None:
    """A g3 group's member_keys carry the g3 key AND every g2 member key
    (the client matches against ANY member key — issue #89 contract)."""
    uk = item("uakino", "Дюна / Dune (2021)", year=2021, n="6268")
    en = item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814")
    groups = merge_results([uk, en])
    keys = list(dict.fromkeys(item_group_key(s) for s in groups[0].sources))
    assert any(k.startswith("g3:") for k in keys)
    assert any(k.startswith("g2:") for k in keys)


def test_form_does_not_divide_g3_groups() -> None:
    """One tt = one work (spec #395 decision 1): a movie and a series
    asserting the same tt merge — form does not divide the g3 tier."""
    groups = merge_results([
        item("yts", "Dune", media_type="movie", year=2021, n="tt8367814", imdb="tt8367814"),
        item("p1", "Dune", media_type="series", year=2000, n="tt8367814", imdb="tt8367814"),
    ])
    assert len(groups) == 1
    assert groups[0].key == group_key_from_imdb("tt8367814")


def test_singleton_with_imdb_is_g3() -> None:
    """One yts item alone keys g3 — its identity IS the tt number."""
    groups = merge_results([item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814")])
    assert groups[0].key == group_key_from_imdb("tt8367814")


def test_imdb_without_tt_prefix_is_rejected() -> None:
    """Only provider-asserted ``tt``-shaped ids enter the tier."""
    groups = merge_results([
        item("p1", "Дюна / Dune (2021)", year=2021, n="x1", imdb="12345"),
        item("p2", "Dune (2021)", year=2021, n="x2", imdb="12345"),
    ])
    assert len(groups) == 1
    assert not groups[0].key.startswith("g3:")


# ---------------------------------------------------------------------------
# Two-namespace grammar
# ---------------------------------------------------------------------------


def test_is_group_key_accepts_both_namespaces() -> None:
    g3 = group_key_from_imdb("tt8367814")
    assert is_group_key(g3)
    assert is_group_key("g2:abcdef0123456789")
    assert not is_group_key("yts:tt8367814")
    assert not is_group_key("uakino:6268:e1")


def test_prefix_constants_distinct() -> None:
    assert GROUP_KEY_PREFIX == "g2:"
    assert GROUP_KEY_PREFIX_G3 == "g3:"


def test_parse_playable_id_unchanged_all_shapes() -> None:
    """Standing parity gate: every pre-existing id shape parses exactly
    as before the g3 tier existed (default grammar = IMDb externals,
    the composing lane's boundary shape)."""
    from cs_uk_api.wire_identity import MOVIE_SUFFIX

    # movie sentinel, both spellings
    assert parse_playable_id(f"yts:tt8367814{MOVIE_SUFFIX}", provider="yts") == ("tt8367814", None)
    assert parse_playable_id(f"tt8367814{MOVIE_SUFFIX}", provider="yts") == ("tt8367814", None)
    # episode wire id (season rides the tail — the discriminator)
    assert parse_playable_id("yts:tt8740758:s1e2", provider="yts") == ("tt8740758", 1)
    assert parse_playable_id("tt8740758:s2e2", provider="yts") == ("tt8740758", 2)
    # a g2 group key is NOT playable
    assert parse_playable_id("g2:abcdef0123456789", provider="uakino") == (None, None)


def test_parse_playable_id_rejects_g3_key() -> None:
    """A g3 key is a group identity, never playable on its own."""
    g3 = group_key_from_imdb("tt8367814")
    assert parse_playable_id(g3, provider="yts") == (None, None)
    assert parse_playable_id(f"{g3}:__movie__", provider="yts") == (None, None)


# ---------------------------------------------------------------------------
# Alias preference (cyrillic wins the card)
# ---------------------------------------------------------------------------


def test_card_alias_prefers_cyrillic_member() -> None:
    """The household's language titles the merged card — even when the
    cyrillic member is NOT first-seen (spec #395's added rule)."""
    from cs_uk_api.merge import MergeGroup
    from cs_uk_api.wire_identity import project_group

    uk = item("uakino", "Дюна / Dune (2021)", year=2021, n="6268")
    en = item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814")
    proj = project_group(MergeGroup(key=group_key_from_imdb("tt8367814"), sources=(en, uk)))
    assert proj.title == "Дюна / Dune (2021)"


def test_card_alias_falls_back_to_first_seen_when_no_cyrillic() -> None:
    from cs_uk_api.merge import MergeGroup
    from cs_uk_api.wire_identity import project_group

    en1 = item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814")
    en2 = item("yts2", "Dune (2021)", year=2021, n="tt8367814x", imdb="tt8367814")
    proj = project_group(MergeGroup(key=group_key_from_imdb("tt8367814"), sources=(en1, en2)))
    assert proj.title == "Dune"


# ---------------------------------------------------------------------------
# State: no migration, new writes land under g3
# ---------------------------------------------------------------------------


def test_resume_written_on_g3_card_persists(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume position recorded against the g3 key re-surfaces on the
    g3 card — the new namespace is the wire-facing key (no migration)."""
    from cs_uk_api.resume_store import ResumeStore

    store = ResumeStore(str(tmp_path / "resume.json"))
    g3 = group_key_from_imdb("tt8367814")
    store.record(g3, position_ticks=900)
    assert g3 in store.entries()
    assert g3 in store.recent(5)


def test_preexisting_g2_resume_does_not_surface_on_g3_card() -> None:
    """The documented loss, asserted so a future migration is a
    conscious change: the g2-era entry stays under its own key and the
    g3 card starts fresh."""
    from cs_uk_api.resume_store import ResumeStore

    store = ResumeStore(None)  # memory-only
    store.record("g2:abcdef0123456789", position_ticks=42)
    g3 = group_key_from_imdb("tt8367814")
    assert g3 not in store.entries()
    assert "g2:abcdef0123456789" in store.entries()


def test_dub_memory_on_g3_group_round_trips() -> None:
    """Dub memory keys on the series group key — a g3 series group is a
    legal memory key (the g3 tier is group identity, period)."""
    from cs_uk_api.user_state import UserStateStore

    store = UserStateStore(None)
    g3 = group_key_from_imdb("tt0944947")
    store.remember_dub(g3, "English")
    assert store.dub_for(g3) == "English"


# ---------------------------------------------------------------------------
# Wire: yts asserts ids; group resolution round-trips g3
# ---------------------------------------------------------------------------


def test_yts_items_carry_imdb(monkeypatch: pytest.MonkeyPatch) -> None:
    """The yts adapter asserts its IMDb ids on both lanes (movies +
    series) — the provider-asserted threshold from the spec."""
    from cs_uk_api.providers.yts import YtsProvider

    p = YtsProvider()  # offline wiring, the established test pattern
    movie = p._card(
        _movie_record("tt8367814")
    )
    assert movie is not None and movie.imdb_id == "tt8367814"
    series = p._show_card(_show_record("tt8740758"))
    assert series is not None and series.imdb_id == "tt8740758"


def _movie_record(imdb: str) -> Any:
    from cs_uk_api.providers.popcorn import PopcornMovie

    return PopcornMovie(
        imdb=imdb,
        title="Dune",
        year=2021,
        genres=["sci-fi"],
        poster="https://cdn/p.jpg",
        rating=8.0,
        description="",
        torrents=[],
    )


def _show_record(imdb: str) -> dict[str, Any]:
    return {
        "imdb_id": imdb,
        "title": "Chernobyl",
        "year": "2019",
        "slug": "chernobyl",
        "description": "",
        "images": {"poster": "https://cdn/c.jpg"},
        "rating": {"percentage": 95.0},
        "episodes": [],
    }


def test_g3_key_round_trips_group_resolution() -> None:
    """A g3 group key resolves through the SAME sources_cache map the
    g2 keys use — via the real search-registration path (namespace-blind
    after registration)."""
    from cs_uk_api._catalog_state import _stores as stores
    from cs_uk_api._catalog_state import resolution
    from cs_uk_api.models import SearchGroup
    from cs_uk_api.wire_identity import project_group

    uk = item("uakino", "Дюна / Dune (2021)", year=2021, n="6268")
    en = item("yts", "Dune", year=2021, n="tt8367814", imdb="tt8367814")
    (mg,) = merge_results([uk, en])
    proj = project_group(mg)
    g3 = proj.key
    stores.sources_cache.clear()
    resolution.register_search_groups([
        SearchGroup(
            group_key=proj.key,
            title=proj.title,
            year=proj.year,
            poster=proj.poster,
            form=proj.form,
            styles=proj.styles,
            genres=list(proj.genres),
            sources=list(proj.sources),
            member_keys=list(proj.member_keys),
        )
    ])
    try:
        resolved = resolution.resolve_group(g3)
        assert resolved is not None
        assert set(resolved) == {"uakino", "yts"}
        # both namespaces point at the same group map
        assert resolution.resolve_group(item_group_key(uk)) == resolved
    finally:
        stores.sources_cache.clear()



# ---------------------------------------------------------------------------
# Facade floor: the g3 card through the REAL app (spec #395 wire pins)
# ---------------------------------------------------------------------------


def test_facade_floor_g3_card_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search → facade Items → detail → PlaybackInfo → stream on a g3
    card: the English movie's IMDb identity UNIFIES with the Ukrainian
    listing in the same search response, the merged card answers by its
    g3 key with the cyrillic-preferred alias, and playback rides the
    merged provider union (the real uakino Дюна conversation) — the
    whole cross-language arc on the real routes."""
    import json as _json
    import pathlib
    from urllib.parse import quote

    import respx
    from fastapi.testclient import TestClient

    from cs_uk_api import main as main_mod
    from cs_uk_api._catalog_state import (
        blocklist_cache,
        content_cache,
        home_cache,
        resolution,
        sources_cache,
    )
    from cs_uk_api.health import TRACKER
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.uakino import UakinoProvider
    from cs_uk_api.providers.yts import YtsProvider
    from cs_uk_api.torrent_engine import EngineStream, FakeTorrentEngine

    FIX = pathlib.Path(__file__).parent / "fixtures"
    LAN = "http://bitplay.lan:3347/api/v1/torrent/s01/stream/2"
    from cs_uk_api.config import SETTINGS

    TOKEN = SETTINGS.jellyfin_token

    list_payload = _json.loads((FIX / "yts" / "search_dune.json").read_text())
    # The movie dialect's base is yts.gg directly (the popcorn knob is the
    # SERIES host — with it unset the movie lane still runs).
    # Keep ONE movie (Dune) so the group arithmetic is exact.
    list_payload["data"]["movies"] = list_payload["data"]["movies"][:1]
    movie = list_payload["data"]["movies"][0]
    magnet = movie["torrents"][0]["url"]
    details = (FIX / "yts" / "details_tt1160419.json").read_text()
    playlists = (FIX / "uakino" / "playlists_movie.json").read_text()
    content_page = (FIX / "uakino" / "content_movie.html").read_text()
    stream_page = (
        "<html><script>file:'https://ashdi.vip/vod/89434/playlist.m3u8';</script></html>"
    )
    search_html = (
        '<div class="movie-item short-item">'
        '<a class="movie-title" href="https://uakino.best/filmy/12567-dyuna.html">Дюна / Dune (2021)</a>'
        '<div class="movie-img"><img src="/uploads/posts/2021-09/dyuna.jpg"></div>'
        '<div class="movie-desk-item"><div class="fi-label">Рік виходу:</div>'
        '<div class="deck-value">2021</div></div>'
        "</div>"
    )

    # The popcorn knob stays unset: the movie dialect's base is yts.gg
    # directly, and the series lane degrades to the movies-only envelope
    # (the runbook's documented default when no series host is wired).

    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
        cache.clear()
    try:
        # The Ukrainian lane rides its browser-session protocol seam; the
        # established FakeSession serves the real fixture bodies per path.
        from cs_uk_api.tests.test_uakino import FakeSession

        session = FakeSession(
            **{
                "/index.php": (200, search_html),
                "/filmy/12567-dyuna.html": (200, content_page),
                "/engine/ajax/playlists.php": (200, playlists),
            }
        )
        PROVIDERS["uakino"] = UakinoProvider(session=session)
        session.ready_event.set()
        # The fan-out's readiness gate reads the process-wide session seam
        # (main + resolution bindings — the test_search_grouping pattern).
        saved_main_session = main_mod.get_session
        main_mod.get_session = lambda: session
        saved_res_session = resolution.get_session
        resolution.get_session = lambda: session
        PROVIDERS["yts"] = YtsProvider(
            engine=FakeTorrentEngine(streams={magnet: EngineStream(url=LAN, container="mp4")})
        )

        client = TestClient(main_mod.app)
        with respx.mock(assert_all_called=False) as router:
            router.get(url="https://yts.gg/api/v2/list_movies.json?").respond(
                200,
                text=_json.dumps(
                    {"status": "ok", "data": {"movies": list_payload["data"]["movies"]}}
                ),
            )
            router.get(url="https://yts.gg/api/v2/movie_details.json?").respond(200, text=details)
            router.get(url="https://ashdi.vip/vod/89434").respond(200, text=stream_page)
            router.get(url="https://ashdi.vip/vod/89434/playlist.m3u8").respond(
                200,
                text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1280000\nsd/index.m3u8\n",
            )

            # -- native search: ONE unified g3 group --------------------------
            r = client.get("/api/search", params={"q": "dune"})
            assert r.status_code == 200, r.text
            groups = r.json()["groups"]
            assert len(groups) == 1, [g["group_key"] for g in groups]
            g = groups[0]
            assert g["group_key"] == "g3:tt1160419"
            assert g["title"] == "Дюна / Dune (2021)"  # cyrillic-preferred alias
            assert {s["provider"] for s in g["sources"]} == {"uakino", "yts"}
            gkey = g["group_key"]

            # -- facade: the g3 card serves, details, plays --------------------
            detail = client.get(
                f"/Items/{quote(gkey, safe='')}", headers={"X-Emby-Token": TOKEN}
            )
            assert detail.status_code == 200, detail.text
            assert detail.json()["Id"] == gkey

            info = client.post(
                f"/Items/{quote(gkey, safe='')}/PlaybackInfo",
                params={"userId": "u"},
                headers={"X-Emby-Token": TOKEN},
                json={},
            )
            assert info.status_code == 200, info.text
            media = info.json()["MediaSources"]
            # One MediaSource per playable VOICE of the merged card's
            # content (the movie's two dubs), each a /Videos stream path.
            assert len(media) == 2
            assert all(m["Path"].startswith("/Videos/") for m in media)

            stream = client.get(
                f"/Videos/{quote(gkey, safe='')}/stream",
                headers={"X-Emby-Token": TOKEN},
                follow_redirects=False,
            )
            assert stream.status_code == 200, stream.text
            assert "#EXTM3U" in stream.text

        # The successful playback recorded lane health (item-vs-lane rule
        # intact through the g3 tier).
        from cs_uk_api.models import STATUS_OK

        assert TRACKER.status("uakino") == STATUS_OK
    finally:
        main_mod.get_session = saved_main_session
        resolution.get_session = saved_res_session
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
            cache.clear()
