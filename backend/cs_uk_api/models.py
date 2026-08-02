from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MediaType = Literal["movie", "series", "anime", "cartoon", "dorama"]
StreamType = Literal["mp4", "m3u8", "hls", "dash"]
TranslationLevel = Literal["content", "episode"]


class SearchResult(BaseModel):
    id: str
    provider: str
    type: MediaType
    title: str
    year: int | None = None
    poster: str | None = None
    url: str


class ProviderFailure(BaseModel):
    """Per-provider failure attribution for /api/search (ADR-0002).

    A provider that contributes nothing to the search response — either
    because it raised an exception or because the overall budget expired
    before it could complete — surfaces as a row here. A provider that
    returns ``[]`` with no exception is a legitimate "no match" answer
    and is NOT a failure.

    The ``code`` field uses a small closed vocabulary:
      - ``"timeout"`` — the per-provider httpx 8s timeout fired, or the
        overall 12s budget expired before this provider completed
        (synthetic row, per ADR-0002).
      - ``"upstream_unreachable"`` — anything else (HTTP error, parse
        failure, scraper bug, etc.).
      - ``"internal"`` — a programming-error escapee (BaseException
        subclass that ``run()`` did not catch). Should be empty in
        healthy operation; if it ever populates, that's a bug.
    """

    provider: str
    code: str
    message: str  # str(exc); human-readable, surfaced for debugging only


class SearchGroup(BaseModel):
    """One merged cross-provider card (issue #71).

    Mirrors ``HomeItem`` (issue #70) at the row level — same
    group_key contract, same first-seen-wins canonical fields — but
    also carries the full per-provider ``sources`` list so the UI can
    render the merged-source label and switch sources when the user
    opens the merged detail screen.

    The ``group_key`` is the same stateless identity used by
    ``/api/content/{group_key}`` (issue #70): a client that picks a
    card from /api/search can drive the detail screen with the
    ``group_key`` field as-is, no translation.
    """

    group_key: str
    title: str
    year: int | None = None
    type: MediaType
    poster: str | None = None
    #: Per-provider ``SearchResult`` rows that collapsed into this
    #: group. Always non-empty (an empty group was dropped upstream).
    #: Order = first-seen in the merge pass; the first source also
    #: wins the canonical title/year/type/poster fields above.
    sources: list[SearchResult]


class SearchResponse(BaseModel):
    query: str = Field(min_length=1, max_length=80)
    #: Merged cross-provider groups (issue #71). One entry per
    #: group_key — same title from N providers collapses into one
    #: ``SearchGroup`` carrying all N ``sources``. The UI renders one
    #: card per group and uses ``sources`` to drive source-switching
    #: into ``/api/content/{group_key}`` (issue #70).
    groups: list[SearchGroup]
    #: Per-provider failure attribution (ADR-0002, issue #81).
    #: Empty list is omitted from JSON by the route via ``exclude_unset``
    #: semantics: clients that don't know about the field see today's
    #: shape unchanged. When non-empty, each entry carries
    #: ``{provider, code, message}`` where ``code`` is one of
    #: ``"timeout"`` / ``"upstream_unreachable"`` / ``"internal"`` and
    #: ``message`` is the surfaced exception string for debugging only.
    failures: list[ProviderFailure] = Field(default_factory=list)


class Translation(BaseModel):
    id: str
    label: str


class Episode(BaseModel):
    number: int
    id: str
    title: str
    # Per-episode translations (v2 spec). When None, fall back to the
    # content-level translations from the parent ContentResponse.
    translations: list[Translation] | None = None


class Season(BaseModel):
    number: int
    episodes: list[Episode]


class ContentResponse(BaseModel):
    id: str
    type: MediaType
    title: str
    year: int | None = None
    description: str = ""
    poster: str | None = None
    translations: list[Translation] = Field(min_length=1)
    seasons: list[Season] | None = None
    translations_level: TranslationLevel = "content"
    country: str | None = None
    #: Stateless cross-provider group identity (issue #69, v3 spec §4.3):
    #: the same title yields the same key from any provider. Client resume/
    #: memory records anchor on this, not on the provider-scoped id.
    group_key: str = ""


class StreamResponse(BaseModel):
    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)


#: Wire status literals (v3 spec §2.1.3/§3.4) — the single source of truth:
#: health.py imports these; no second copy of the strings exists anywhere.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_DOWN = "down"

HealthStatus = Literal[STATUS_OK, STATUS_DEGRADED, STATUS_DOWN]


class ProviderInfo(BaseModel):
    id: str
    name: str
    types: list[MediaType]
    status: HealthStatus = "ok"
    last_error_at: str | None = None


class Section(BaseModel):
    id: str
    title: str
    type: MediaType


class ProviderSections(BaseModel):
    provider: str
    name: str
    sections: list[Section]


class BrowseResponse(BaseModel):
    provider: str
    section: str
    page: int = Field(ge=1)
    has_next: bool
    results: list[SearchResult]


class ErrorResponse(BaseModel):
    error: str
    message: str


# ---------------------------------------------------------------------------
# Home API (issue #70)
# ---------------------------------------------------------------------------
#
# ``HomeItem`` is the merged cross-provider identity of one title: a
# stable groupKey plus the list of provider ids that contributed to it
# (dedup is by groupKey — same title from two providers is one row, and
# the client receives the union of providers that surfaced it). The
# ``type`` and ``poster`` fields are sourced from the first-seen
# provider; the spec doesn't preserve attribution at the field level,
# only at the row level (via ``providers``).
#
# ``HomeRow`` aggregates ``HomeItem`` rows under a human label. The
# ``type`` field doubles as a routing key for the row's contents:
#   - ``"newest"`` — «Новинки», aggregated across providers that opt into
#     ``newest_section``.
#   - ``"popular"`` — «Популярні зараз», only when animeon's ``popular``
#     browse returned data (issue #70 AC).
#   - a media-type literal (``"movie"``, ``"series"``, ``"anime"``,
#     ``"cartoon"``, ``"dorama"``) — one row per type, aggregating every
#     provider section whose ``Section.type`` matches.
#
# ``GroupContentResponse`` is the ``/api/content/{groupKey}`` payload:
# the merged item plus the full providers list. It deliberately mirrors
# ``HomeItem`` plus a single field rather than re-defining a separate
# Content shape — the spec asks for "the merged item with its source
# providers", nothing more.


class HomeItem(BaseModel):
    group_key: str
    title: str
    year: int | None = None
    type: MediaType
    poster: str | None = None
    #: Provider ids that contributed this row. Always non-empty (a row
    #: with zero providers was dropped upstream). Order = round-robin
    #: visit order across providers; first-seen wins for the title
    #: fields.
    providers: list[str] = Field(default_factory=list)


class HomeRow(BaseModel):
    #: Human-readable label: «Новинки», «Популярні зараз», «Фільми», etc.
    title: str
    #: Routing key — see module docstring.
    type: str
    items: list[HomeItem]


class HomeResponse(BaseModel):
    rows: list[HomeRow]


class GroupContentResponse(BaseModel):
    """``/api/content/{groupKey}`` payload: one merged item + providers."""
    item: HomeItem
    providers: list[str]
