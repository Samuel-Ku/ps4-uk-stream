"""The shared play-request seam: one provider attempt, both outcome axes.

The native and facade route suites pin their own envelopes. This file
pins the shared interface both of them cross: parsing, the translation
gate, health classification, engine-path attribution and the fact that
the attested code is the one the calling surface reports.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cs_uk_api.health import TRACKER
from cs_uk_api.models import StreamResponse
from cs_uk_api.play_attestation import PLAY_ATTESTATION
from cs_uk_api.play_request import attempt_play, playable_target
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError
from cs_uk_api.torrent_engine import ENGINE_TRACKER_ID

_URL = "https://cdn.example.test/v.mp4"


class _Stub(BaseProvider):
    id = "p1"
    name = "P1"
    types = ("series",)

    def __init__(
        self,
        outcome: StreamResponse | Exception,
        *,
        allowed: list[str] | None = None,
    ) -> None:
        self.outcome = outcome
        self.allowed = allowed
        self.stream_calls: list[tuple[str, str | None]] = []
        self.translation_calls = 0

    async def search(self, query: str, http: Any) -> list[Any]:
        return []

    async def content(self, external_id: str, http: Any) -> Any:
        raise AssertionError("content is not part of this seam")

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> StreamResponse:
        self.stream_calls.append((content_id, translation))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def episode_translations(
        self, content_id: str, http: Any
    ) -> list[str] | None:
        self.translation_calls += 1
        return self.allowed


def _native_code(code: str | None) -> str:
    passthrough = {"invalid_translation", "translation_missing", "gated"}
    return code if code in passthrough else "upstream_unreachable"


def test_playable_target_splits_on_the_first_colon_only() -> None:
    PROVIDERS["p1"] = _Stub(StreamResponse(url=_URL))
    assert playable_target("p1:series-1:s1e1") == ("p1", "series-1:s1e1")
    assert playable_target("ghost:series-1") is None
    assert playable_target("p1:") is None


@pytest.mark.asyncio
async def test_translation_gate_refuses_before_the_stream_call() -> None:
    stub = _Stub(StreamResponse(url=_URL), allowed=["uk"])
    PROVIDERS["p1"] = stub

    async with httpx.AsyncClient() as http:
        outcome = await attempt_play(
            "p1",
            "series-1:s1e1",
            "de",
            http,
            item_id="p1:series-1:s1e1",
            surface="native",
            refusal_code=_native_code,
        )

    assert outcome.served is False
    assert outcome.code == "invalid_translation"
    assert stub.stream_calls == []
    assert stub.translation_calls == 1
    assert TRACKER._samples.get("p1") is None
    assert PLAY_ATTESTATION.snapshot().providers["p1"].refused_codes == {
        "invalid_translation": 1
    }


@pytest.mark.asyncio
async def test_item_verdict_attests_the_surface_code_not_lane_health() -> None:
    stub = _Stub(ProviderError("not_found", "dead swarm"))
    PROVIDERS["p1"] = stub

    async with httpx.AsyncClient() as http:
        outcome = await attempt_play(
            "p1",
            "movie-1",
            None,
            http,
            item_id="p1:movie-1",
            surface="native",
            refusal_code=_native_code,
        )

    assert outcome.code == "not_found"
    assert TRACKER._samples.get("p1") is None
    assert PLAY_ATTESTATION.snapshot().providers["p1"].refused_codes == {
        "upstream_unreachable": 1
    }


@pytest.mark.asyncio
async def test_engine_path_fault_attributes_health_and_attestation_to_engine() -> None:
    stub = _Stub(ProviderError("unreachable", "engine down").with_engine_path())
    PROVIDERS["p1"] = stub

    async with httpx.AsyncClient() as http:
        outcome = await attempt_play(
            "p1",
            "movie-1",
            None,
            http,
            item_id="p1:movie-1",
            surface="stream",
        )

    assert outcome.code == "unreachable"
    assert "p1" not in TRACKER._samples
    assert list(TRACKER._samples[ENGINE_TRACKER_ID])[-1] is False
    assert PLAY_ATTESTATION.snapshot().providers[ENGINE_TRACKER_ID].refused_codes == {
        "unreachable": 1
    }


@pytest.mark.asyncio
async def test_success_records_lane_health_and_only_declared_surfaces() -> None:
    stub = _Stub(StreamResponse(url=_URL))
    PROVIDERS["p1"] = stub

    async with httpx.AsyncClient() as http:
        quiet = await attempt_play(
            "p1",
            "movie-1",
            None,
            http,
            item_id="p1:movie-1",
            surface=None,
        )

    assert quiet.served is True
    assert list(TRACKER._samples["p1"])[-1] is True
    assert PLAY_ATTESTATION.snapshot().providers == {}

    async with httpx.AsyncClient() as http:
        played = await attempt_play(
            "p1",
            "movie-1",
            None,
            http,
            item_id="p1:movie-1",
            surface="native",
        )

    assert played.served is True
    assert PLAY_ATTESTATION.snapshot().providers["p1"].served == 1
