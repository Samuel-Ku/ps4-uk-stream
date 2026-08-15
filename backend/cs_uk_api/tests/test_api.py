import asyncio

from fastapi.testclient import TestClient

from cs_uk_api import catalog_state, uakino_browser
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
    from cs_uk_api.models import ContentResponse, StreamResponse, Translation
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider

    class _Epi(BaseProvider):
        id = "epi-test"
        name = "Epi"
        types = ("series",)

        async def search(self, query, http):
            return []

        async def content(self, external_id, http):
            return ContentResponse(
                id=external_id,
                form="series",
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
    from cs_uk_api.models import ContentResponse, Translation
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider

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
                form="series",
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


def test_lifespan_closes_uakino_session():
    """Regression: `UakinoSession` is lazily created on first request,
    so the FastAPI lifespan shutdown must close it. Without this hook
    SIGTERM orphans the headless Chromium child process.

    Stub the module-level factory before the TestClient enters/exits
    `with` so the on-exit lifespan handler invokes our close hook.
    """
    closes: list[None] = []

    class _StubSession:
        """Minimal ``UakinoSessionProtocol``: the lifespan warm task calls
        ``warm()`` + ``heartbeat_loop()`` and ``/api/providers`` reads
        ``ready_event`` (issue #193/#195)."""

        def __init__(self) -> None:
            self.ready_event = asyncio.Event()

        async def warm(self) -> None:
            pass

        async def heartbeat_loop(self, record):  # type: ignore[no-untyped-def]
            while True:
                await asyncio.sleep(3600)

        async def close(self) -> None:
            closes.append(None)

    saved = uakino_browser._session
    uakino_browser._session = _StubSession()  # type: ignore[assignment]
    try:
        with TestClient(app) as tc:
            tc.get("/api/providers")
    finally:
        uakino_browser._session = saved
    assert closes == [None]


def test_content_route_blocks_russian_country(monkeypatch):
    """When CS_UK_BLOCK_RUSSIAN is enabled and the provider returns a
    country in the blocklist, /api/content must return 404 and cache
    the block so a second request short-circuits without hitting upstream.
    """
    from cs_uk_api.models import ContentResponse, Translation
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider

    class _Russian(BaseProvider):
        id = "russian-test"
        name = "RussianTest"
        types = ("movie",)

        async def search(self, query, http):
            return []

        async def content(self, external_id, http):
            return ContentResponse(
                id=f"russian-test:{external_id}",
                form="movie",
                title="Блокированный",
                translations=[Translation(id="uk", label="UK")],
                country="росія",
            )

        async def stream(self, content_id, translation, http):
            raise AssertionError("unused")

    PROVIDERS["russian-test"] = _Russian()  # type: ignore[assignment]
    try:
        r = client.get("/api/content/russian-test:12345-blocked")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "not_found"
        r2 = client.get("/api/content/russian-test:12345-blocked")
        assert r2.status_code == 404
    finally:
        del PROVIDERS["russian-test"]


def test_content_route_allows_non_russian_country(monkeypatch):
    """Content with a non-Russian country passes through normally."""
    from cs_uk_api.models import ContentResponse, Translation
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider

    class _Ukr(BaseProvider):
        id = "ukr-test"
        name = "UkrTest"
        types = ("movie",)

        async def search(self, query, http):
            return []

        async def content(self, external_id, http):
            return ContentResponse(
                id=f"ukr-test:{external_id}",
                form="movie",
                title="Не блокувати",
                translations=[Translation(id="uk", label="UK")],
                country="україна",
            )

        async def stream(self, content_id, translation, http):
            raise AssertionError("unused")

    PROVIDERS["ukr-test"] = _Ukr()  # type: ignore[assignment]
    try:
        r = client.get("/api/content/ukr-test:12345-ok")
        assert r.status_code == 200
        assert r.json()["country"] == "україна"
    finally:
        del PROVIDERS["ukr-test"]


def test_content_route_passes_open_when_country_unknown(monkeypatch):
    """When country is None (unknown), content passes through
    — fail-open contract."""
    from cs_uk_api.models import ContentResponse, Translation
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider

    class _Unknown(BaseProvider):
        id = "unknown-test"
        name = "UnknownTest"
        types = ("movie",)

        async def search(self, query, http):
            return []

        async def content(self, external_id, http):
            return ContentResponse(
                id=f"unknown-test:{external_id}",
                form="movie",
                title="Невідома країна",
                translations=[Translation(id="uk", label="UK")],
                country=None,
            )

        async def stream(self, content_id, translation, http):
            raise AssertionError("unused")

    PROVIDERS["unknown-test"] = _Unknown()
    try:
        r = client.get("/api/content/unknown-test:12345")
        assert r.status_code == 200
        assert r.json()["country"] is None
    finally:
        del PROVIDERS["unknown-test"]


def test_content_route_respects_block_russian_disabled(monkeypatch):
    """When block_russian is disabled, Russian content returns 200."""
    import cs_uk_api.config as config_mod
    from cs_uk_api.models import ContentResponse, Translation
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import BaseProvider

    original = config_mod.SETTINGS
    config_mod.SETTINGS = type(original)(
        host=original.host,
        port=original.port,
        upstream_timeout_s=original.upstream_timeout_s,
        search_total_timeout_s=original.search_total_timeout_s,
        poster_size_cap_bytes=original.poster_size_cap_bytes,
        poster_allowed_hosts=original.poster_allowed_hosts,
        cache_search_s=original.cache_search_s,
        cache_content_s=original.cache_content_s,
        cache_home_s=original.cache_home_s,
        cache_poster_s=original.cache_poster_s,
        cache_gated_s=original.cache_gated_s,
        poster_cache_dir=original.poster_cache_dir,
        poster_disk_ttl_s=original.poster_disk_ttl_s,
        providers=original.providers,
        block_russian=False,
        home_row_limit=original.home_row_limit,
    )
    try:
        catalog_state.blocklist_cache.clear()
        catalog_state.content_cache.clear()

        class _Russian(BaseProvider):
            id = "russian-disabled-test"
            name = "RussianDisabledTest"
            types = ("movie",)

            async def search(self, query, http):
                return []

            async def content(self, external_id, http):
                return ContentResponse(
                    id=f"russian-disabled-test:{external_id}",
                    form="movie",
                    title="Разрешено",
                    translations=[Translation(id="uk", label="UK")],
                    country="росія",
                )

            async def stream(self, content_id, translation, http):
                raise AssertionError("unused")

        PROVIDERS["russian-disabled-test"] = _Russian()
        try:
            r = client.get("/api/content/russian-disabled-test:999")
            assert r.status_code == 200
            assert r.json()["country"] == "росія"
        finally:
            del PROVIDERS["russian-disabled-test"]
            catalog_state.blocklist_cache.clear()
            catalog_state.content_cache.clear()
    finally:
        config_mod.SETTINGS = original