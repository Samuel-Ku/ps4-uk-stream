"""Drift probe harness tests (spec #285, ticket #286).

The listing probe runs through the REAL adapter seam (a stub provider
here, real providers in the live smoke run): its browse/search cards
are parsed objects, so form/style and field expectations are computed
from exactly what the API would surface. Deep probe checks
content → stream → HEAD, with gated treated as healthy and 4xx/5xx
HEADs as failures. Rotation covers each provider every N days without
overlapping.
"""

from __future__ import annotations

from typing import Any

import pytest

from cs_uk_api.drift.probe import (
    EXCLUDED_PROVIDER_IDS,
    listing_section,
    probe_deep,
    probe_listing,
    rotate_deep_providers,
)
from cs_uk_api.models import SearchResult, StreamResponse
from cs_uk_api.providers.base import BaseProvider, ProviderError


def _card(pid: str, i: int, *, form: str = "movie", url: str | None = None) -> SearchResult:
    return SearchResult(
        id=f"{pid}:ext-{i}",
        provider=pid,
        form=form,  # type: ignore[arg-type]
        title=f"Title {i}",
        url=url or f"https://{pid}.example/{i}",
    )


class _Stub(BaseProvider):
    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(
        self,
        *,
        cards: list[SearchResult] | None = None,
        browse_error: Exception | None = None,
        stream_ok: bool = True,
        stream_status: int = 200,
        gated: bool = False,
    ) -> None:
        self._cards = cards or [_card("p1", 1), _card("p1", 2)]
        self._browse_error = browse_error
        self._stream_ok = stream_ok
        self._stream_status = stream_status
        self._gated = gated
        self.sections = ()

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        if self._browse_error is not None:
            raise self._browse_error
        return list(self._cards)

    async def browse(self, section: str, page: int, http: Any) -> tuple[list[SearchResult], bool]:
        if self._browse_error is not None:
            raise self._browse_error
        return list(self._cards), False

    async def content(self, external_id: str, http: Any) -> Any:
        from cs_uk_api.models import ContentResponse, Translation

        if self._gated:
            raise ProviderError("gated", "for subscribers")
        return ContentResponse(
            id=f"p1:{external_id}",
            form="movie",  # type: ignore[arg-type]
            title="Title",
            translations=[Translation(id="uk", label="Дубляж")],
        )

    async def stream(self, content_id: str, translation: str | None, http: Any) -> StreamResponse:
        if not self._stream_ok:
            raise ProviderError("not_found", "no stream")
        return StreamResponse(url="https://cdn.example.test/v.mp4", type="mp4", headers={})


class _HeadClient:
    """Minimal httpx-like client: records HEAD calls, answers a status."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.head_calls: list[str] = []

    async def head(self, url: str) -> Any:
        self.head_calls.append(url)

        class _R:
            status_code = self.status

        return _R()


# ------------------------------------------------------------ listing


@pytest.mark.asyncio
async def test_listing_probe_parses_through_adapter() -> None:
    """The listing probe returns the adapter's own parsed cards."""
    stub = _Stub(cards=[_card("p1", 1, form="movie"), _card("p1", 2, form="series")])
    result = await probe_listing(stub, _HeadClient())

    assert result.ok
    assert result.kind == "browse"
    assert len(result.cards) == 2
    assert {c.form for c in result.cards} == {"movie", "series"}


@pytest.mark.asyncio
async def test_listing_probe_error_is_recorded_not_raised() -> None:
    """A raising browse/search becomes ok=False with the error text."""
    stub = _Stub(browse_error=ProviderError("timeout", "upstream slow"))
    result = await probe_listing(stub, _HeadClient())

    assert not result.ok
    assert "timeout" in (result.error or "")


@pytest.mark.asyncio
async def test_listing_probe_unexpected_error_recorded() -> None:
    """A non-ProviderError (parse bug) is also recorded, not raised."""
    stub = _Stub(browse_error=ValueError("boom"))
    result = await probe_listing(stub, _HeadClient())

    assert not result.ok
    assert "ValueError" in (result.error or "")


def test_listing_section_prefers_newest() -> None:
    stub = _Stub()
    stub.sections = ()  # only newest_section matters here
    assert listing_section(stub) == ("page", "browse")


def test_listing_section_falls_back_to_first_declared() -> None:
    class _S(BaseProvider):
        id = "p2"
        name = "P2"
        types = ("movie",)

        async def search(self, query: str, http: Any) -> list[SearchResult]:
            return []

        async def content(self, external_id: str, http: Any) -> Any:
            raise NotImplementedError

        async def stream(self, content_id: str, translation: str | None, http: Any) -> StreamResponse:
            raise NotImplementedError

    _S.sections = ()  # type: ignore[assignment]
    # Sections are dataclass-ish class attrs on providers; give it one.
    from cs_uk_api.models import Section

    _S.sections = (Section(id="films", title="Фільми"),)  # type: ignore[assignment]
    assert listing_section(_S()) == ("films", "browse")


# --------------------------------------------------------------- deep


@pytest.mark.asyncio
async def test_deep_probe_content_stream_head_ok() -> None:
    """A 2xx HEAD on the stream URL makes the deep probe healthy."""
    stub = _Stub()
    http = _HeadClient(status=200)
    result = await probe_deep(stub, http, _card("p1", 1))

    assert result.ok
    assert result.stream_url == "https://cdn.example.test/v.mp4"
    assert result.head_status == 200
    assert http.head_calls == ["https://cdn.example.test/v.mp4"]


@pytest.mark.asyncio
async def test_deep_probe_bad_head_status_is_failure() -> None:
    """A 4xx/5xx HEAD marks the provider failed (drift-worthy)."""
    stub = _Stub()
    result = await probe_deep(stub, _HeadClient(status=404), _card("p1", 1))

    assert not result.ok
    assert "404" in (result.error or "")


@pytest.mark.asyncio
async def test_deep_probe_stream_raise_is_failure() -> None:
    """A stream() raise (animeon's lost URLs) fails the deep probe."""
    stub = _Stub(stream_ok=False)
    result = await probe_deep(stub, _HeadClient(status=200), _card("p1", 1))

    assert not result.ok
    assert "not_found" in (result.error or "")


@pytest.mark.asyncio
async def test_deep_probe_gated_is_healthy() -> None:
    """A gated verdict is the provider's deliberate state, not drift."""
    stub = _Stub(gated=True)
    result = await probe_deep(stub, _HeadClient(status=200), _card("p1", 1))

    assert result.ok
    assert result.error == "gated"


# ------------------------------------------------------------ rotation


def test_rotation_covers_every_provider_every_n_days() -> None:
    ids = [f"p{i}" for i in range(1, 7)]  # 6 providers, 3/day → 2 days
    covered: set[str] = set()
    for day in range(6):
        subset = rotate_deep_providers(ids, day, 3)
        assert len(subset) == 2
        covered |= subset
    assert covered == set(ids)


def test_rotation_excludes_uakino_always() -> None:
    ids = ["uakino", "p1", "p2"]
    for day in range(12):
        subset = rotate_deep_providers(ids, day, 3)
        assert "uakino" not in subset


def test_rotation_empty_with_no_providers() -> None:
    assert rotate_deep_providers([], 0, 6) == set()


def test_excluded_ids_contains_uakino() -> None:
    assert "uakino" in EXCLUDED_PROVIDER_IDS
