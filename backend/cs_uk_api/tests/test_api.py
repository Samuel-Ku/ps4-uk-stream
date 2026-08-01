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
