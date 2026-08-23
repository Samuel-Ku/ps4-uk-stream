"""LLM taste-profile layer tests (spec #290, ticket #292).

Parser: a valid v1 profile parses; wrong version, malformed shapes,
and out-of-band weights reject the WHOLE profile (never partial);
row ideas beyond 2 are truncated. Client: a respx-mocked endpoint
installs a validated profile; missing knobs, non-JSON answers and
network errors all yield None. The module accessors (get/set) are
pinned for the later wiring slices.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from cs_uk_api import llm as llm_mod
from cs_uk_api.config import SETTINGS
from cs_uk_api.llm import (
    MAX_ROW_IDEAS,
    ProfileError,
    active_profile,
    fetch_profile,
    parse_profile,
    set_active_profile,
)
from cs_uk_api.models import TasteProfile
from cs_uk_api.recommend import ItemProfile

_GOOD = {
    "v": 1,
    "genre_weights": {"драма": 1.8, "комедія": 0.4},
    "theme_tags": ["похмурі", "повільні"],
    "row_ideas": [
        {"title": "Похмурі драми для тебе", "genres": ["драма"], "max": 10},
        {"title": "Серіали без гумору", "genres": ["драма", "детектив"], "max": 8},
    ],
}


# ------------------------------------------------------------ parser


def test_valid_profile_parses() -> None:
    p = parse_profile(_GOOD)
    assert p.genre_weights == {"драма": 1.8, "комедія": 0.4}
    assert p.theme_tags == ("похмурі", "повільні")
    assert len(p.row_ideas) == 2
    assert p.row_ideas[0].title == "Похмурі драми для тебе"
    assert p.row_ideas[0].genres == ("драма",)
    assert p.row_ideas[0].max == 10


def test_wrong_version_rejects_whole_profile() -> None:
    with pytest.raises(ProfileError):
        parse_profile({**_GOOD, "v": 2})


def test_non_dict_rejects() -> None:
    with pytest.raises(ProfileError):
        parse_profile([1, 2, 3])


def test_out_of_band_weight_rejects() -> None:
    with pytest.raises(ProfileError):
        parse_profile({**_GOOD, "genre_weights": {"драма": 3.0}})
    with pytest.raises(ProfileError):
        parse_profile({**_GOOD, "genre_weights": {"драма": 0.1}})


def test_non_numeric_weight_rejects() -> None:
    with pytest.raises(ProfileError):
        parse_profile({**_GOOD, "genre_weights": {"драма": "high"}})


def test_malformed_row_idea_rejects() -> None:
    bad = {**_GOOD, "row_ideas": [{"title": "X", "genres": ["драма"]}]}  # no max
    with pytest.raises(ProfileError):
        parse_profile(bad)


def test_more_than_two_ideas_truncated() -> None:
    ideas = [
        {"title": f"Ряд {i}", "genres": ["драма"], "max": 5} for i in range(5)
    ]
    p = parse_profile({**_GOOD, "row_ideas": ideas})
    assert len(p.row_ideas) == MAX_ROW_IDEAS


def test_empty_profile_valid() -> None:
    p = parse_profile({"v": 1})
    assert p.genre_weights == {}
    assert p.theme_tags == ()
    assert p.row_ideas == ()


# ------------------------------------------------------------ accessors


def test_active_profile_accessors() -> None:
    set_active_profile(None)
    assert active_profile() is None
    prof = TasteProfile(genre_weights={"драма": 1.5})
    set_active_profile(prof)
    assert active_profile() is prof
    set_active_profile(None)


def test_llm_enabled_requires_all_knobs(monkeypatch) -> None:
    from dataclasses import replace

    disabled = replace(SETTINGS, llm_base_url=None, llm_key=None, llm_model=None)
    monkeypatch.setattr(llm_mod, "SETTINGS", disabled)
    assert llm_mod.llm_enabled() is False

    full = replace(
        SETTINGS,
        llm_base_url="https://api.example.test/v1",
        llm_key="k",
        llm_model="m",
    )
    monkeypatch.setattr(llm_mod, "SETTINGS", full)
    assert llm_mod.llm_enabled() is True


# ------------------------------------------------------------ client


def _extract_good_answer() -> str:
    import json

    return json.dumps(_GOOD, ensure_ascii=False)


class _FakeClient:
    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self, messages: list[dict[str, str]], *, timeout: float = 30.0
    ) -> str:
        self.calls.append(messages)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.asyncio
async def test_fetch_profile_returns_validated_profile(monkeypatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    client = _FakeClient(_extract_good_answer())
    p = await fetch_profile(
        history=[{"title": "Т", "genres": ["драма"], "year": 2021}],
        queries=["дюна"],
        genres=["драма", "комедія"],
        client=client,  # type: ignore[arg-type]
    )
    assert p is not None
    assert p.genre_weights == {"драма": 1.8, "комедія": 0.4}
    # One call, system + user messages.
    assert len(client.calls) == 1
    assert client.calls[0][0]["role"] == "system"


@pytest.mark.asyncio
async def test_fetch_profile_disabled_knobs_returns_none(monkeypatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url=None))
    client = _FakeClient("unused")
    p = await fetch_profile(history=[], queries=[], genres=[], client=client)  # type: ignore[arg-type]
    assert p is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_fetch_profile_network_error_returns_none(monkeypatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    p = await fetch_profile(
        history=[], queries=[], genres=[],
        client=_FakeClient(httpx.ConnectError("boom")),  # type: ignore[arg-type]
    )
    assert p is None


@pytest.mark.asyncio
async def test_fetch_profile_non_json_returns_none(monkeypatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    p = await fetch_profile(
        history=[], queries=[], genres=[],
        client=_FakeClient("not json at all"),  # type: ignore[arg-type]
    )
    assert p is None


@pytest.mark.asyncio
async def test_fetch_profile_invalid_profile_returns_none(monkeypatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    p = await fetch_profile(
        history=[], queries=[], genres=[],
        client=_FakeClient('{"v": 99, "genre_weights": {}}'),  # type: ignore[arg-type]
    )
    assert p is None


@pytest.mark.asyncio
async def test_fetch_profile_fenced_json_parses(monkeypatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    fenced = f"```json\n{_extract_good_answer()}\n```"
    p = await fetch_profile(
        history=[], queries=[], genres=[], client=_FakeClient(fenced)  # type: ignore[arg-type]
    )
    assert p is not None
    assert p.genre_weights == {"драма": 1.8, "комедія": 0.4}


@pytest.mark.asyncio
async def test_httpx_client_hits_chat_completions(monkeypatch) -> None:
    """The real client posts one request with the bearer key (respx)."""
    from dataclasses import replace

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    with respx.mock() as mlock:
        route = mlock.post("https://api.example.test/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": _extract_good_answer()}}]
                },
            )
        )
        p = await fetch_profile(
            history=[], queries=[], genres=[],
            client=llm_mod.HttpxLlmClient(
                base_url="https://api.example.test/v1",
                api_key="secret",
                model="test-model",
                http=httpx.AsyncClient(),
            ),
        )
    assert p is not None
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer secret"
    assert req.read().decode().count("test-model") >= 0  # body includes the model
    body = __import__("json").loads(req.read().decode())
    assert body["model"] == "test-model"
    assert "temperature" in body


# -------------------------------------------------- refresh wiring (#294)


def _seed_refresh_signals(cs: Any) -> None:
    """Playback history + a search query + warm profiles — the signals
    the refresh collects for the model call."""
    cs.install_profiles(
        {
            "g2:test": ItemProfile(
                genres=frozenset({"драма", "трилер"}),
                people=frozenset(),
                year=2021,
                form="movie",
                styles=frozenset(),
            ),
            "g2:other": ItemProfile(
                genres=frozenset({"комедія"}),
                people=frozenset(),
                year=2019,
                form="movie",
                styles=frozenset(),
            ),
        }
    )
    cs.record_playback("g2:test", 1_000)
    cs.record_search_query("дюна")


@pytest.mark.asyncio
async def test_refresh_profile_installs_and_clears_home(monkeypatch) -> None:
    """#294 AC1: the refresh collects the signals, calls the model,
    installs the active profile, and invalidates the home snapshot so
    the new rows surface on the next build — True on install."""
    from dataclasses import replace

    import cs_uk_api._catalog_state as cs

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    from cs_uk_api.models import HomeItem, HomeResponse, HomeRow

    _seed_refresh_signals(cs)
    set_active_profile(None)
    cs.home_cache.set(  # pretend a home is cached (a real shape — the
        "home:v1",  # history collector reads its rows for titles)
        HomeResponse(
            rows=[
                HomeRow(
                    title="Фільми",
                    type="movie",
                    items=[
                        HomeItem(
                            group_key="g2:test",
                            title="Тестовий фільм",
                            year=2021,
                            poster=None,
                            form="movie",
                            styles=[],
                            genres=["драма"],
                            providers=["p1"],
                        )
                    ],
                )
            ]
        ),
    )
    try:
        ok = await cs.refresh_profile(client=_FakeClient(_extract_good_answer()))
        assert ok is True
        p = active_profile()
        assert p is not None
        assert p.genre_weights == {"драма": 1.8, "комедія": 0.4}
        assert cs.home_cache.get("home:v1") is None  # invalidated
    finally:
        set_active_profile(None)
        cs.install_profiles({})
        cs.clear_playback()
        cs.home_cache.clear()


@pytest.mark.asyncio
async def test_refresh_profile_failure_keeps_previous(monkeypatch) -> None:
    """#294 AC1 / spec user story 8: a network error or a non-JSON model
    answer leaves the PREVIOUS profile active and returns False — a
    broken model can't ruin the current taste state."""
    from dataclasses import replace

    import cs_uk_api._catalog_state as cs

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url="x", llm_key="k", llm_model="m")
    )
    _seed_refresh_signals(cs)
    previous = TasteProfile(genre_weights={"драма": 1.5})
    set_active_profile(previous)
    try:
        assert (
            await cs.refresh_profile(client=_FakeClient(httpx.ConnectError("boom")))
            is False
        )
        assert active_profile() is previous
        assert (
            await cs.refresh_profile(client=_FakeClient("not json at all")) is False
        )
        assert active_profile() is previous
    finally:
        set_active_profile(None)
        cs.install_profiles({})
        cs.clear_playback()
        cs.home_cache.clear()


@pytest.mark.asyncio
async def test_refresh_profile_disabled_knobs_returns_false(monkeypatch) -> None:
    """#294: without the three knobs the refresh is inert — no LLM
    call, no profile, False (the layer is invisible until enabled)."""
    from dataclasses import replace

    import cs_uk_api._catalog_state as cs

    monkeypatch.setattr(
        llm_mod, "SETTINGS", replace(SETTINGS, llm_base_url=None, llm_key=None, llm_model=None)
    )
    _seed_refresh_signals(cs)
    set_active_profile(None)
    try:
        assert await cs.refresh_profile(client=_FakeClient(_extract_good_answer())) is False
        assert active_profile() is None
    finally:
        set_active_profile(None)
        cs.install_profiles({})
        cs.clear_playback()
        cs.home_cache.clear()
