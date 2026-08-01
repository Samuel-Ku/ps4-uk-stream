from fastapi.testclient import TestClient

from cs_uk_api.main import app

client = TestClient(app)


def test_providers_endpoint_lists_uakino():
    r = client.get("/api/providers")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()]
    assert "uakino" in ids


def test_registry_registers_all_landed_adapters():
    """Q1 (grilling): every adapter with a landed file + passing tests
    must be registered, so the live gate can exercise them."""
    r = client.get("/api/providers")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()]
    for expected in ("uakino", "ufdub", "unimay", "kinotron", "cikavaideya", "hentaiukr", "bambooua", "kinovezha"):
        assert expected in ids


def test_search_validates_empty_query():
    r = client.get("/api/search?q=")
    assert r.status_code == 422


def test_search_rejects_unknown_provider():
    r = client.get("/api/search?q=foo&provider=ghost")
    assert r.status_code == 400


def test_unknown_content_returns_404():
    r = client.get("/api/content/nope:nope")
    assert r.status_code == 404


def test_stream_rejects_invalid_translation_via_provider_hook(monkeypatch):
    """If a provider reports per-episode translations, /api/stream must
    reject unknown translations before fetching the upstream URL."""
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider
    from cs_uk_api.models import ContentResponse, StreamResponse, Translation

    class _Epi(BaseProvider):
        id = "epi-test"
        name = "Epi"
        types = ("series",)

        async def search(self, query, http):
            return []

        async def content(self, external_id, http):
            return ContentResponse(
                id=external_id,
                type="series",
                title="T",
                translations=[Translation(id="uk", label="UK")],
                translations_level="episode",
            )

        async def stream(self, content_id, translation, http):
            return StreamResponse(url="https://x.example/v.mp4", type="mp4")

        async def episode_translations(self, content_id, http):
            return ["uk", "en"]

    PROVIDERS["epi-test"] = _Epi()  # type: ignore[assignment]
    try:
        r = client.get("/api/stream/epi-test:s1e1?translation=de")
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_translation"

        r2 = client.get("/api/stream/epi-test:s1e1?translation=uk")
        assert r2.status_code == 200
        assert r2.json()["url"] == "https://x.example/v.mp4"
    finally:
        del PROVIDERS["epi-test"]


def test_content_route_accepts_slash_in_content_id(monkeypatch):
    """Live-gate regression (2026-08-01): bambooua content ids carry a
    slash (`bambooua:dorama/722-story-of-kunning-palace`), which the
    plain `{content_id}` route path parameter rejects. The external_id
    must reach the provider intact."""
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider
    from cs_uk_api.models import ContentResponse, Translation

    seen: list[str] = []

    class _Slash(BaseProvider):
        id = "slash-test"
        name = "Slash"
        types = ("movie", "series")

        async def search(self, query, http):
            return []

        async def content(self, external_id, http):
            seen.append(external_id)
            return ContentResponse(
                id=f"slash-test:{external_id}",
                type="series",
                title="T",
                translations=[Translation(id="uk", label="UK")],
            )

        async def stream(self, content_id, translation, http):
            raise AssertionError("unused")

    PROVIDERS["slash-test"] = _Slash()  # type: ignore[assignment]
    try:
        r = client.get("/api/content/slash-test:dorama/722-story-of-kunning-palace")
        assert r.status_code == 200
        assert seen == ["dorama/722-story-of-kunning-palace"]
    finally:
        del PROVIDERS["slash-test"]
