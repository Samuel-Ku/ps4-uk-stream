import pytest

from cs_uk_api.providers.base import BaseProvider, ProviderError


class Dummy(BaseProvider):
    id = "dummy"
    name = "Dummy"
    types = ("movie", "series")

    async def search(self, query, http):  # type: ignore[override]
        raise ProviderError("upstream_unreachable", "site down")

    async def content(self, external_id, http):  # type: ignore[override]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[override]
        raise NotImplementedError


@pytest.mark.asyncio
async def test_provider_error_carries_code_and_message():
    p = Dummy()
    try:
        await p.search("anything", http=None)  # type: ignore[arg-type]
    except ProviderError as e:
        assert e.code == "upstream_unreachable"
        assert e.message == "site down"
    else:
        pytest.fail("expected ProviderError")
