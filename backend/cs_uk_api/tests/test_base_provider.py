import httpx
import pytest

from cs_uk_api.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderErrorCode,
    split_content_id,
    split_content_suffix,
)


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


class DummyHosts(Dummy):
    #: Declares its upstream hosts once; guarded_get must apply this
    #: allowlist by default (US7) without a per-call opt-in.
    hosts = frozenset({"anime.example"})


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


# --- T6: typed error-code vocabulary (US6) ---------------------------------


def test_error_code_vocab_covers_the_wire_contract():
    """The vocab pins the wire codes consumers must not re-type as literals."""
    assert ProviderErrorCode.NOT_FOUND.value == "not_found"
    assert ProviderErrorCode.PARSE_FAILED.value == "parse_failed"
    assert ProviderErrorCode.UNREACHABLE.value == "unreachable"
    assert ProviderErrorCode.UPSTREAM_UNREACHABLE.value == "upstream_unreachable"
    assert ProviderErrorCode.GATED.value == "gated"
    assert ProviderErrorCode.TRANSLATION_MISSING.value == "translation_missing"
    assert ProviderErrorCode.INVALID_TRANSLATION.value == "invalid_translation"
    assert ProviderErrorCode.TIMEOUT.value == "timeout"
    assert ProviderErrorCode.SCRAPE_FAILED.value == "scrape_failed"


def test_error_code_members_are_str_subclasses():
    """A literal-raised error must still compare equal to the constant."""
    for member in ProviderErrorCode:
        assert isinstance(member, str)
        assert member == member.value
        assert f"{member.value}" == member.value


def test_error_code_matches_literal_raised_error():
    err = ProviderError("upstream_unreachable", "site down")
    assert err.code == ProviderErrorCode.UPSTREAM_UNREACHABLE
    assert err.code in (ProviderErrorCode.UNREACHABLE, ProviderErrorCode.UPSTREAM_UNREACHABLE)


# --- T6: shared content-id splitter (US6) ----------------------------------


def test_split_content_id_plain():
    assert split_content_id("ufdub:dorama-408") == ("ufdub", "dorama-408")


def test_split_content_id_movie_sentinel():
    assert split_content_id("kinotron:12345:__movie__") == ("kinotron", "12345")


def test_split_content_id_episode_tail_season():
    assert split_content_id("ufdub:dorama-408-1:s1e1") == ("ufdub", "dorama-408-1")


def test_split_content_id_episode_tail_plain():
    assert split_content_id("uakino:42055:e5") == ("uakino", "42055")


def test_split_content_id_episode_tail_blob():
    assert split_content_id("animeon:918:e1:eyJhbGciOiJIUzI1NiJ9") == ("animeon", "918")


def test_split_content_id_malformed():
    assert split_content_id("no-colon") == ("", "")
    assert split_content_id("") == ("", "")


# --- T7: prefix-stripped suffix splitter -----------------------------------


def test_split_content_suffix_bare():
    assert split_content_suffix("film-48-fokus-pokus-hocus-pocus") == (
        "film-48-fokus-pokus-hocus-pocus",
        "",
    )


def test_split_content_suffix_movie_sentinel():
    assert split_content_suffix("film-48-fokus-pokus:__movie__") == (
        "film-48-fokus-pokus",
        "",
    )


def test_split_content_suffix_episode_tail_season():
    assert split_content_suffix("10496-mesniki:s1e3") == ("10496-mesniki", "s1e3")


def test_split_content_suffix_episode_tail_plain():
    assert split_content_suffix("226-jak-vlashtovanij-vsesvit:e2") == (
        "226-jak-vlashtovanij-vsesvit",
        "e2",
    )


def test_split_content_suffix_unknown_suffix_passes_through():
    # A colon that is not a recognized suffix form is not split — the
    # adapter's slug validator rejects the composite as not_found.
    assert split_content_suffix("film-48-x:weird") == ("film-48-x:weird", "")


# --- T6: guarded_get default allowlist (US7) -------------------------------


class StubHttp:
    """A minimal httpx client stand-in: records calls, fails on fetch."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise AssertionError(f"unexpected fetch: {url}")


class StubHttpOk(StubHttp):
    """Lets a fetch through and returns a canned response."""

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, text="ok")


@pytest.mark.asyncio
async def test_guarded_get_fail_closed_without_hosts():
    """No declared hosts = the guard is the default, so the fetch is blocked."""
    p = Dummy()  # hosts == frozenset() → fail-closed
    http = StubHttp()
    with pytest.raises(ProviderError) as exc:
        await p.guarded_get(http, "http://evil.example/x")
    assert exc.value.code == ProviderErrorCode.NOT_FOUND
    assert http.calls == []


@pytest.mark.asyncio
async def test_guarded_get_applies_declared_hosts_by_default():
    """Declared hosts are the default allowlist — no per-call opt-in needed."""
    p = DummyHosts()
    http = StubHttpOk()
    await p.guarded_get(http, "http://anime.example/s")
    assert http.calls == [("http://anime.example/s", {"follow_redirects": False, "headers": None, "params": None})]


@pytest.mark.asyncio
async def test_guarded_get_blocks_host_outside_declared_allowlist():
    p = DummyHosts()
    http = StubHttp()
    with pytest.raises(ProviderError) as exc:
        await p.guarded_get(http, "http://evil.example/x")
    assert exc.value.code == ProviderErrorCode.NOT_FOUND
    assert http.calls == []


@pytest.mark.asyncio
async def test_guarded_get_escape_hatch_overrides_default():
    """A per-call allowed_hosts replaces the declared default."""
    p = DummyHosts()
    http = StubHttpOk()
    await p.guarded_get(http, "http://other.example/x", allowed_hosts={"other.example"})
    assert http.calls == [("http://other.example/x", {"follow_redirects": False, "headers": None, "params": None})]


@pytest.mark.asyncio
async def test_guarded_get_none_escape_hatch_skips_check():
    """Explicit None is the documented escape hatch: the check is skipped."""
    p = Dummy()  # empty hosts, but None opts out of the guard
    http = StubHttpOk()
    await p.guarded_get(http, "http://anything.example/x", allowed_hosts=None)
    assert http.calls == [("http://anything.example/x", {"follow_redirects": False, "headers": None, "params": None})]
