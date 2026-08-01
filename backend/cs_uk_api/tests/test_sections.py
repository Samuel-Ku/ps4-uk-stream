from fastapi.testclient import TestClient

from cs_uk_api.main import app

client = TestClient(app)


def test_sections_lists_uakino_with_sections():
    r = client.get("/api/sections")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    by_id = {p["id"]: p for p in data}
    assert "uakino" in by_id
    assert by_id["uakino"]["name"] == "Uakino"
    sections = by_id["uakino"]["sections"]
    assert isinstance(sections, list)
    assert len(sections) > 0
    s0 = sections[0]
    assert {"id", "title", "type"} <= set(s0.keys())


def test_sections_omits_providers_without_sections():
    """A provider with sections=() should not appear in /api/sections."""
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider

    # ensure uakino exposes sections (sanity check)
    p = PROVIDERS["uakino"]
    assert len(p.sections) > 0

    # a hypothetical provider with no sections would be omitted — register
    # an in-memory provider to confirm the route's filter logic.
    class _Empty(BaseProvider):
        id = "empty-test"
        name = "Empty"
        types = ("movie",)
        sections = ()

        async def search(self, query, http):
            return []

        async def content(self, external_id, http):
            raise NotImplementedError

        async def stream(self, content_id, translation, http):
            raise NotImplementedError

    PROVIDERS["empty-test"] = _Empty()  # type: ignore[assignment]
    try:
        r = client.get("/api/sections")
        ids = [p["id"] for p in r.json()]
        assert "empty-test" not in ids
    finally:
        del PROVIDERS["empty-test"]


def test_browse_returns_results_for_uakino_section():
    """Browse a Uakino section and confirm pagination + result shape."""
    import pathlib

    import httpx
    import respx

    from cs_uk_api.providers import PROVIDERS

    fixture = (pathlib.Path(__file__).parent / "fixtures" / "uakino" / "browse_filmy.html").read_text(encoding="utf-8")
    p = PROVIDERS["uakino"]
    first_section_id = p.sections[0].id
    with respx.mock(assert_all_called=False) as router:
        router.get(host="uakino.club", path__startswith="/filmy/").respond(200, text=fixture)
        # The endpoint caches per (provider, section, page), so the cache key
        # must be unique across tests; clear it for the page=1 case.
        from cs_uk_api import main as m
        m._browse_cache.clear()
        r = client.get(f"/api/browse?provider=uakino&section={first_section_id}&page=1")
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "uakino"
    assert data["section"] == first_section_id
    assert data["page"] == 1
    assert data["has_next"] is True
    assert len(data["results"]) == 2
    titles = [x["title"] for x in data["results"]]
    assert any("Дюна" in t for t in titles)


def test_browse_unknown_provider_returns_400():
    r = client.get("/api/browse?provider=ghost&section=filmy&page=1")
    assert r.status_code == 400


def test_browse_unknown_section_returns_404():
    r = client.get("/api/browse?provider=uakino&section=nope&page=1")
    assert r.status_code == 404


def test_browse_page_must_be_positive():
    r = client.get("/api/browse?provider=uakino&section=filmy&page=0")
    assert r.status_code == 422