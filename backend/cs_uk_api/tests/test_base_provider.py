import pathlib

import pytest

from cs_uk_api.models import StreamResponse
from cs_uk_api.providers.base import BaseProvider, ProviderError, provider_stream_response


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


def test_provider_stream_response_is_the_classic_wire_shape():
    """One owner for ``url + type + stream_headers(referer)``: the
    factory composes the SAME two headers the 16 call sites used to
    re-type, so the wire stays byte-identical."""
    resp = provider_stream_response(
        "https://cdn.example/master.m3u8", "m3u8", "https://ashdi.vip/"
    )
    assert isinstance(resp, StreamResponse)
    assert resp.url == "https://cdn.example/master.m3u8"
    assert resp.type == "m3u8"
    assert resp.headers == {
        "Referer": "https://ashdi.vip/",
        "User-Agent": "cs-uk-api/1.0",
    }
    # Classic providers never set the engine-dialect fields.
    assert resp.seekable is None
    assert resp.subtitle_url is None
    assert resp.allowed_domains == frozenset()


def test_provider_stream_response_sanctions_foreign_cdn_domains():
    """ufdub's 302 gateway may hand bytes to a foreign CDN — the optional
    ``allowed_domains`` keeps that D7 declaration on the factory."""
    resp = provider_stream_response(
        "https://media.example/v.mp4",
        "mp4",
        "https://ufdub.com/",
        allowed_domains=frozenset({"dropboxusercontent.com"}),
    )
    assert resp.allowed_domains == frozenset({"dropboxusercontent.com"})


def test_stream_response_construction_stays_with_its_dialect_owner():
    """Ownership drift pin: a NEW ``StreamResponse(`` construction must
    land in its dialect's owner module, not sprout in provider files.
    ``providers/base.py`` owns the classic shape; ``torrent_lane.py``
    owns the engine shape (``seekable``/``subtitle_url``); the allowlist
    below is the closed set of raw-construction dialect files — ufdub's
    foreign-CDN declaration rides the factory's ``allowed_domains``.

    To add a site: put it in an owner, then extend the allowlist with a
    comment naming the dialect."""
    providers_dir = pathlib.Path(__file__).parent.parent / "providers"
    assert sorted(
        p.name
        for p in providers_dir.glob("*.py")
        if p.name not in ("base.py", "__init__.py")
        and "StreamResponse(" in p.read_text(encoding="utf-8")
    ) == ["animeon.py", "eneyida.py", "uaserialspro.py"]
