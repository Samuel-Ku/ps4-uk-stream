import pathlib

import pytest
import respx

from cs_uk_api.providers.uakino import UakinoProvider

FIXTURE = (pathlib.Path(__file__).parent / "fixtures" / "uakino" / "search.html").read_text(
    encoding="utf-8"
)


@pytest.mark.asyncio
async def test_uakino_search_parses_results():
    import httpx
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uakino.club/search/").respond(200, text=FIXTURE)
        async with httpx.AsyncClient(headers={"User-Agent": "test"}) as http:
            results = await UakinoProvider().search("дюна", http)
    assert len(results) == 2
    assert results[0].id == "uakino:film-dune-2021"
    assert results[0].title.startswith("Дюна")
    assert results[0].type == "movie"
    assert results[0].poster is not None
    assert results[1].type == "series"
