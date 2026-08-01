from fastapi.testclient import TestClient

from cs_uk_api.main import app

client = TestClient(app)


def test_providers_endpoint_lists_uakino():
    r = client.get("/api/providers")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()]
    assert "uakino" in ids


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
                translation_level="episode",
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
