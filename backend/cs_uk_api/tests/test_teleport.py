"""Semantic Source Teleport — backend contract (spec #443).

The route is tested through HTTP with a fake aligner, so the default
suite needs neither ffmpeg nor Chromaprint. The matcher is tested as a
pure function over synthetic fingerprint sequences; the real
ffmpeg/fpcalc wiring stays an opt-in integration check.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
import struct
import wave
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import config as _config
from cs_uk_api._catalog_state import register_search_groups
from cs_uk_api.catalog import ContentVerdict, ProviderContent
from cs_uk_api.main import app
from cs_uk_api.models import (
    ContentResponse,
    Episode,
    SearchGroup,
    SearchResult,
    Season,
    StreamResponse,
    Translation,
)
from cs_uk_api.native import teleport as teleport_route
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError
from cs_uk_api.teleport import (
    AlignmentResult,
    ChromaprintAligner,
    TeleportUnavailable,
    match_fingerprints,
)

GROUP = "g2:teleport-test"
ITEM = "p1:show:s1e1"
CANDIDATE = "p2:show:s1e1"


def _wav(seconds: float = 15.0, rate: int = 8000) -> bytes:
    frames = int(seconds * rate)
    payload = b"".join(struct.pack("<h", (i * 37) % 2000 - 1000) for i in range(frames))
    out = BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(payload)
    return out.getvalue()


class _Provider(BaseProvider):
    id = "p1"
    name = "Provider One"
    types = ("series",)

    def __init__(self, *, error: Exception | None = None) -> None:
        self.stream_calls: list[str] = []
        self.error = error
        self.sections = ()

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def content(self, external_id: str, http: Any) -> Any:
        raise AssertionError("not used by teleport validation")

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> StreamResponse:
        self.stream_calls.append(content_id)
        if self.error is not None:
            raise self.error
        return StreamResponse(
            url="https://cdn.example.test/secret-token/master.m3u8",
            type="m3u8",
            headers={"Referer": "https://provider.example.test/"},
        )


class _FakeAligner:
    def __init__(self, result: AlignmentResult | None = None) -> None:
        self.result = result or AlignmentResult(
            "matched", offset_seconds=12.5, confidence=0.93, margin=0.21
        )
        self.calls: list[dict[str, Any]] = []

    async def align(
        self,
        *,
        reference_wav: bytes,
        candidate: StreamResponse,
        position_seconds: float,
    ) -> AlignmentResult:
        self.calls.append(
            {
                "reference_wav": reference_wav,
                "candidate": candidate,
                "position_seconds": position_seconds,
            }
        )
        return self.result


class _UnavailableAligner:
    async def align(self, **kwargs: Any) -> AlignmentResult:
        raise TeleportUnavailable("fpcalc missing")


class _TimeoutAligner:
    async def align(self, **kwargs: Any) -> AlignmentResult:
        await asyncio.sleep(0.05)
        return AlignmentResult("no_match", reason="low_confidence")


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch):
    p1 = _Provider()
    p2 = _Provider()
    p2.id = "p2"
    p2.name = "Provider Two"
    monkeypatch.setitem(PROVIDERS, "p1", p1)
    monkeypatch.setitem(PROVIDERS, "p2", p2)
    register_search_groups(
        [
            SearchGroup(
                group_key=GROUP,
                title="Show",
                form="series",
                sources=[
                    SearchResult(
                        id="p1:show",
                        provider="p1",
                        form="series",
                        title="Show",
                        url="https://p1.example.test/show",
                    ),
                    SearchResult(
                        id="p2:show",
                        provider="p2",
                        form="series",
                        title="Show",
                        url="https://p2.example.test/show",
                    ),
                ],
                member_keys=[GROUP],
            )
        ]
    )
    yield p1, p2


def _post(
    client: TestClient,
    *,
    item_id: str = ITEM,
    candidate_id: str = CANDIDATE,
    position_ticks: int = 600_000_000,
    audio: bytes | None = None,
    window_seconds: float = 15.0,
):
    return client.post(
        "/api/teleport/align",
        data={
            "item_id": item_id,
            "candidate_id": candidate_id,
            "position_ticks": str(position_ticks),
            "window_seconds": str(window_seconds),
        },
        files={"audio": ("reference.wav", audio or _wav(), "audio/wav")},
    )


def test_matched_returns_candidate_stream_and_seek(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    _p1, p2 = seeded
    fake = _FakeAligner()
    monkeypatch.setattr(teleport_route, "ALIGNER", fake)
    client = TestClient(app)

    response = _post(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "matched"
    assert body["source_id"] == CANDIDATE
    assert body["stream"]["url"].endswith("/master.m3u8")
    assert body["stream"]["headers"] == {"Referer": "https://provider.example.test/"}
    assert body["seek_ticks"] == 600_000_000 + 125_000_000
    assert body["offset_seconds"] == 12.5
    assert body["confidence"] == 0.93
    assert body["candidates"][0]["id"] == CANDIDATE
    assert p2.stream_calls == ["show:s1e1"]
    assert fake.calls[0]["position_seconds"] == 60.0
    assert fake.calls[0]["reference_wav"].startswith(b"RIFF")


def test_ambiguous_does_not_permit_auto_switch_but_carries_suggestion(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAligner(
        AlignmentResult(
            "ambiguous",
            offset_seconds=-3.0,
            confidence=0.72,
            margin=0.01,
            reason="low_confidence",
        )
    )
    monkeypatch.setattr(teleport_route, "ALIGNER", fake)
    body = _post(TestClient(app)).json()
    assert body["status"] == "ambiguous"
    assert body["stream"] is not None
    assert body["seek_ticks"] == 600_000_000 - 30_000_000
    assert body["reason"] == "low_confidence"


def test_no_match_returns_no_stream_or_seek(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAligner(
        AlignmentResult("no_match", confidence=0.41, margin=0.12, reason="low_confidence")
    )
    monkeypatch.setattr(teleport_route, "ALIGNER", fake)
    body = _post(TestClient(app)).json()
    assert body["status"] == "no_match"
    assert body["stream"] is None
    assert body["seek_ticks"] is None
    assert body["offset_seconds"] is None
    assert body["candidates"][0]["confidence"] == 0.41


def test_candidate_from_a_different_episode_is_rejected(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(TestClient(app), candidate_id="p2:show:s1e2")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "candidate_mismatch"


def test_candidate_from_a_different_work_is_rejected(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_search_groups(
        [
            SearchGroup(
                group_key="g2:other",
                title="Other",
                form="series",
                sources=[
                    SearchResult(
                        id="p2:other",
                        provider="p2",
                        form="series",
                        title="Other",
                        url="https://p2.example.test/other",
                    )
                ],
                member_keys=["g2:other"],
            )
        ]
    )
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(TestClient(app), candidate_id="p2:other:s1e1")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "candidate_mismatch"


def _series_content(
    title: str, *, group_key: str, year: int | None = 2011
) -> ContentResponse:
    return ContentResponse(
        id="show",
        form="series",
        title=title,
        year=year,
        group_key=group_key,
        translations=[Translation(id="uk", label="Українська")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(number=1, id="show:e1", title="Серія 1"),
                    Episode(number=2, id="show:e2", title="Серія 2"),
                ],
            )
        ],
    )


def _cold_providers(
    monkeypatch: pytest.MonkeyPatch, contents: dict[str, ContentResponse]
) -> None:
    """Register p1/p2 without touching the group map (a cold process)."""
    p1 = _Provider()
    p2 = _Provider()
    p2.id = "p2"
    p2.name = "Provider Two"
    monkeypatch.setitem(PROVIDERS, "p1", p1)
    monkeypatch.setitem(PROVIDERS, "p2", p2)

    async def content(provider_id: str, external_id: str) -> ProviderContent:
        return ProviderContent(
            verdict=ContentVerdict.OK, content=contents[provider_id]
        )

    monkeypatch.setattr(teleport_route.catalog, "provider_content", content)
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())


def _episode_content(provider_id: str) -> ProviderContent:
    return ProviderContent(
        verdict=ContentVerdict.OK,
        content=ContentResponse(
            id=f"{provider_id}:show",
            form="series",
            title="Show",
            translations=[Translation(id="uk", label="Українська")],
            seasons=[
                Season(
                    number=1,
                    episodes=[
                        Episode(
                            number=1,
                            # Deliberately different id shapes: the
                            # validator must compare numbers, not strings.
                            id=("show:e1" if provider_id == "p1" else ":e1"),
                            title="Серія 1",
                        )
                    ],
                )
            ],
        ),
    )


def test_cold_group_map_resolves_a_valid_pair_from_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cold_providers(
        monkeypatch,
        {
            "p1": _series_content("Show", group_key="g2:show"),
            "p2": _series_content("Show", group_key="g2:show"),
        },
    )
    response = _post(TestClient(app))
    assert response.status_code == 200
    assert response.json()["status"] == "matched"


def test_cold_group_map_accepts_alias_variants_of_the_same_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The merged map spans alias variants; the per-item stateless keys
    # differ, so the fallback must use the merge rule, not key equality.
    _cold_providers(
        monkeypatch,
        {
            "p1": _series_content("Show / Шоу", group_key="g2:show"),
            "p2": _series_content("Шоу", group_key="g2:shou"),
        },
    )
    response = _post(TestClient(app))
    assert response.status_code == 200
    assert response.json()["status"] == "matched"


def test_cold_group_map_rejects_a_different_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cold_providers(
        monkeypatch,
        {
            "p1": _series_content("Show", group_key="g2:show"),
            "p2": _series_content("Other", group_key="g2:other"),
        },
    )
    response = _post(TestClient(app))
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "candidate_mismatch"


def test_cold_group_map_rejects_a_different_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cold_providers(
        monkeypatch,
        {
            "p1": _series_content("Show", group_key="g2:show"),
            "p2": _series_content("Show", group_key="g2:show"),
        },
    )
    response = _post(TestClient(app), candidate_id="p2:show:s1e2")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "candidate_mismatch"


def test_cold_group_map_content_failure_is_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cold_providers(
        monkeypatch,
        {
            "p1": _series_content("Show", group_key="g2:show"),
            "p2": _series_content("Show", group_key="g2:show"),
        },
    )

    async def failing_content(provider_id: str, external_id: str) -> ProviderContent:
        raise ProviderError("upstream_unreachable", "content down")

    monkeypatch.setattr(teleport_route.catalog, "provider_content", failing_content)
    response = _post(TestClient(app))
    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "candidate_unavailable"


def test_bare_episode_tails_validate_by_number(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_provider_content(provider_id: str, external_id: str) -> ProviderContent:
        return _episode_content(provider_id)

    monkeypatch.setattr(teleport_route.catalog, "provider_content", fake_provider_content)
    fake = _FakeAligner()
    monkeypatch.setattr(teleport_route, "ALIGNER", fake)

    response = _post(
        TestClient(app),
        item_id="p1:show:e1",
        candidate_id="p2:show:e1",
    )
    assert response.status_code == 200
    assert response.json()["status"] == "matched"
    assert fake.calls


def test_validation_content_failure_is_502(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_content(provider_id: str, external_id: str) -> ProviderContent:
        raise ProviderError("upstream_unreachable", "content down")

    monkeypatch.setattr(teleport_route.catalog, "provider_content", failing_content)
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(
        TestClient(app),
        item_id="p1:show:e1",
        candidate_id="p2:show:e1",
    )
    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "candidate_unavailable"


def test_candidate_provider_failure_is_502(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    _p1, p2 = seeded
    p2.error = ProviderError("upstream_unreachable", "candidate down")
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(TestClient(app))
    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "candidate_unavailable"


def test_missing_tools_is_503(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _UnavailableAligner())
    response = _post(TestClient(app))
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "teleport_unavailable"


def test_alignment_timeout_is_504(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _TimeoutAligner())
    monkeypatch.setattr(teleport_route, "ALIGNMENT_TIMEOUT_S", 0.01)
    response = _post(TestClient(app))
    assert response.status_code == 504
    assert response.json()["detail"]["error"] == "alignment_timeout"


def test_invalid_audio_is_rejected_before_the_aligner(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAligner()
    monkeypatch.setattr(teleport_route, "ALIGNER", fake)
    response = _post(TestClient(app), audio=b"not a wav")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_audio"
    assert fake.calls == []


def test_too_short_audio_is_rejected(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(TestClient(app), audio=_wav(seconds=5.0))
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_audio"


def test_oversized_audio_is_rejected(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(TestClient(app), audio=b"x" * (4 * 1024 * 1024 + 1))
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "audio_too_large"


def test_disabled_teleport_is_503(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _config, "SETTINGS", replace(_config.SETTINGS, teleport_enabled=False)
    )
    response = _post(TestClient(app))
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "teleport_unavailable"


def test_unplayable_id_is_404(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(TestClient(app), item_id="badid")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_found"


def test_negative_position_is_rejected(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = _post(TestClient(app), position_ticks=-1)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_request"


def test_missing_audio_is_invalid_request(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    response = TestClient(app).post(
        "/api/teleport/align",
        data={
            "item_id": ITEM,
            "candidate_id": CANDIDATE,
            "position_ticks": "600000000",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_request"


def test_facade_and_teleport_share_one_resolution_seam() -> None:
    from cs_uk_api import playback
    from cs_uk_api.jellyfin import delivery, playback_info

    assert delivery.resolve_stream is playback.resolve_stream
    assert playback_info._resolve_stream is playback.resolve_stream


def test_logs_do_not_carry_stream_urls(
    seeded, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(teleport_route, "ALIGNER", _FakeAligner())
    with caplog.at_level(logging.INFO, logger="cs_uk_api.native.teleport"):
        response = _post(TestClient(app))
    assert response.status_code == 200
    assert "secret-token" not in caplog.text
    assert "master.m3u8" not in caplog.text


# --------------------------------------------------------- pure matcher


def _random_hashes(length: int, seed: int = 7) -> list[int]:
    rng = random.Random(seed)
    values: list[int] = []
    while len(values) < length:
        value = rng.getrandbits(32)
        # Keep the middle bits populated so the low-entropy guard passes,
        # but do not reuse a low-nibble mask: unrelated Chromaprint hashes
        # agree on roughly half their bits, and the test data must too.
        if 6 <= value.bit_count() <= 26:
            values.append(value)
    return values


def test_matcher_recovers_a_known_insertion() -> None:
    # 4 hashes/s on both sides: the differential rate derivation must
    # recover it exactly, not absorb Chromaprint's fixed context window.
    reference = _random_hashes(60)
    prefix = 20
    # 60 s at 4 hashes/s, like the reference: the candidate must carry the
    # same frame spacing for the derived rate to be meaningful.
    candidate = [0x0F0F0F0F] * prefix + reference + [0x0F0F0F0F] * 160
    match = match_fingerprints(
        reference,
        candidate,
        reference_duration=15.0,
        candidate_duration=60.0,
    )
    assert match.status == "matched"
    rate = len(candidate) / 60.0
    assert match.candidate_offset_seconds is not None
    assert abs(match.candidate_offset_seconds - prefix / rate) < 0.01
    assert match.confidence is not None and match.confidence > 0.99


def test_matcher_rejects_unrelated_audio() -> None:
    match = match_fingerprints(
        _random_hashes(60, seed=1),
        _random_hashes(180, seed=2),
        reference_duration=15.0,
        candidate_duration=45.0,
    )
    assert match.status == "no_match"
    assert match.confidence is not None and match.confidence < 0.8


def test_matcher_rejects_silence_as_a_perfect_match() -> None:
    match = match_fingerprints(
        [0] * 60,
        [0] * 180,
        reference_duration=15.0,
        candidate_duration=45.0,
    )
    assert match.status == "no_match"
    assert match.reason == "reference_quiet"


def test_matcher_marks_repeated_audio_ambiguous() -> None:
    reference = _random_hashes(60)
    candidate = [0x0F0F0F0F] * 10 + reference + [0x0F0F0F0F] * 10 + reference
    match = match_fingerprints(
        reference,
        candidate,
        reference_duration=15.0,
        candidate_duration=35.0,
    )
    assert match.status == "ambiguous"
    assert match.margin is not None and match.margin < 0.05


# ------------------------------------------------- real-tool integration


def _write_wav(path: Path, payload: bytes, *, rate: int = 11025) -> None:
    with wave.open(str(path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(payload)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("fpcalc") is None,
    reason="teleport integration needs ffmpeg and fpcalc; install the package named in README",
)
def test_aligner_recovers_generated_delay_with_real_tools(tmp_path: Path) -> None:
    """Exercise the real ffmpeg/fpcalc path with a known 20 s insertion.

    The 200 s source keeps the reference after fpcalc's 120 s default
    cut-off, so a missing ``-length 0`` cannot pass this test.
    """
    rate = 11025
    rng = random.Random(42)
    noise = b"".join(
        struct.pack("<h", rng.randint(-12000, 12000)) for _ in range(rate * 200)
    )
    silence = b"\x00\x00" * (rate * 20)
    candidate_path = tmp_path / "candidate.wav"
    _write_wav(candidate_path, silence + noise, rate=rate)
    reference_path = tmp_path / "reference.wav"
    _write_wav(reference_path, noise[-rate * 15 * 2 :], rate=rate)

    aligner = ChromaprintAligner()
    result = asyncio.run(
        aligner.align(
            reference_wav=reference_path.read_bytes(),
            candidate=StreamResponse(url=str(candidate_path), type="mp4", headers={}),
            position_seconds=200.0,
        )
    )

    assert result.status == "matched"
    assert result.offset_seconds is not None
    assert abs(result.offset_seconds - 20.0) < 1.0
    assert result.confidence is not None and result.confidence > 0.80
    assert result.margin is not None and result.margin >= 0.05
