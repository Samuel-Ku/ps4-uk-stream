from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_serializer

# Model B axes (ADR-0001, contract step #135): form is the
# cinematic-vs-episodic split, styles the optional genre tags. Empty
# frozenset = ordinary live-action (decided: empty, not "live"). The
# legacy ``MediaType``/``type`` axis is gone — every content item ships
# exactly ``form`` + ``styles``.
MediaForm = Literal["movie", "series"]
MediaStyle = Literal["anime", "cartoon", "dorama"]
StreamType = Literal["mp4", "m3u8", "hls", "dash"]
TranslationLevel = Literal["content", "episode"]


def _empty_styles() -> frozenset[MediaStyle]:
    return frozenset()


class SearchResult(BaseModel):
    id: str
    provider: str
    # Model B (ADR-0001, contract #135): ``form`` is required — every
    # piece of content is either a movie or a series. ``styles`` is the
    # decided set of genre tags; empty frozenset = ordinary live-action
    # (the decided default, not an absence marker).
    form: MediaForm
    title: str
    year: int | None = None
    poster: str | None = None
    url: str
    styles: frozenset[MediaStyle] = Field(default_factory=_empty_styles)
    #: Free-form genre labels parsed from the listing card (ticket #213),
    #: e.g. ufdub's ``div.short-c`` "Жанр: Аніме / Жахи". Empty list =
    #: the provider's page carries no genre metadata. Flows into the
    #: home snapshot's ``HomeItem.genres`` and the Jellyfin ``/Genres``
    #: shelf + ``genreIds`` filter.
    genres: list[str] = Field(default_factory=list)

    @field_serializer("styles")
    def _ser_styles(self, value: frozenset[MediaStyle]) -> list[str]:
        # frozenset is not JSON-serializable; emit a stable sorted list.
        return sorted(value)


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

    The ``member_keys`` field is the issue #89 reconciliation layer:
    it carries every per-item group key that contributed to this
    merged card. The canonical ``group_key`` is the yearful-preferred-
    min of those — for a year-soft pair (yearful + yearless), the
    min key is the yearful one. A client resume record keyed by the
    yearless member would NOT match ``group_key`` on its own; the
    client matches against any element of ``member_keys`` instead.
    """

    group_key: str
    title: str
    year: int | None = None
    poster: str | None = None
    # Model B (contract #135): first-seen-wins, like the other canonical
    # fields; ``form``/``styles`` come from the first source row. ``form``
    # is required (no legacy ``type`` on the wire anymore).
    form: MediaForm
    styles: frozenset[MediaStyle] = Field(default_factory=_empty_styles)
    #: Genre labels (ticket #213) — first-seen-wins like the other
    #: canonical fields (the merge core's ``sources[0]`` is the group's
    #: first-seen member).
    genres: list[str] = Field(default_factory=list)
    #: Per-provider ``SearchResult`` rows that collapsed into this
    #: group. Always non-empty (an empty group was dropped upstream).
    #: Order = first-seen in the merge pass; the first source also
    #: wins the canonical title/year/form/poster fields above.
    sources: list[SearchResult]
    #: Every per-item group key that contributed to this merged card
    #: (issue #89). First-seen order — the canonical ``group_key``
    #: (``yearful-preferred-min``) is always the first element for
    #: groups with at least one yearful member. Sort key is the
    #: ``g2:`` digest in lexicographic order; ``"g2:"`` prefix sorts
    #: before any other character so the yearful preference still
    #: wins on tie.
    member_keys: list[str] = Field(default_factory=list)

    @field_serializer("styles")
    def _ser_styles(self, value: frozenset[MediaStyle]) -> list[str]:
        # frozenset is not JSON-serializable; emit a stable sorted list.
        return sorted(value)


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


class Person(BaseModel):
    """One cast member on a content page (ticket #221).

    ``id`` is provider-scoped and stable — the person's own page slug
    when the site exposes one (uaserialspro ``/person/<id>-<slug>/``,
    kinotron ``/xfsearch/actors/<name>/``), else a name-based key
    (klontv's JSON-LD has names only) — so the Jellyfin People rail can
    round-trip it through ``/Persons/{id}`` without a second lookup.
    """

    id: str
    name: str
    role: str = "Actor"


class Episode(BaseModel):
    number: int
    id: str
    title: str
    # Per-episode translations (v2 spec). When None, fall back to the
    # content-level translations from the parent ContentResponse.
    translations: list[Translation] | None = None
    # Ticket #223: per-episode metadata the provider exposes (animeon's
    # ``episodes-info`` carries real titles + air dates). Empty/None =
    # the provider has no per-episode data; the episode DTO then omits
    # the field (Switchfin renders a bare row, as before).
    description: str = ""
    premiere_date: str | None = None


class Season(BaseModel):
    number: int
    episodes: list[Episode]


class ContentResponse(BaseModel):
    id: str
    # Model B (ADR-0001, contract #135): ``form`` is required — the
    # Movie/Series verdict every consumer (facade, filters, merge)
    # reads. The legacy ``type`` axis is gone.
    form: MediaForm
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
    styles: frozenset[MediaStyle] = Field(default_factory=_empty_styles)
    #: Genre labels (ticket #213) — mirrors ``SearchResult.genres`` so
    #: the detail surface and the genre shelf share one source.
    genres: list[str] = Field(default_factory=list)
    #: Cast (ticket #221) — parsed from the provider's content page
    #: where it exposes one (kinotron/uaserialspro actor lists, klontv
    #: JSON-LD). Empty list = the provider's page carries no cast; the
    #: detail DTO then omits ``People`` and the app hides the rail.
    people: list[Person] = Field(default_factory=list)
    #: Community rating on the provider's 0-10 scale (ticket #222) —
    #: klontv's schema.org ``aggregateRating.ratingValue`` is the only
    #: real score exposed so far (ufdub/kinotron show +/- vote deltas,
    #: not ratings). None = no rating on the page; the detail DTO then
    #: omits ``CommunityRating`` and the badge stays hidden.
    rating: float | None = None

    @field_serializer("styles")
    def _ser_styles(self, value: frozenset[MediaStyle]) -> list[str]:
        # frozenset is not JSON-serializable; emit a stable sorted list.
        return sorted(value)


class StreamResponse(BaseModel):
    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)
    #: Registrable domains the provider sanctions beyond ``url``'s own
    #: host: a 302 gateway (e.g. ufdub's VIDEOS.php) may hand bytes to a
    #: foreign CDN the provider picked (Dropbox). The stream proxy's CDN
    #: check (D7 SSRF posture) honours these in addition to the
    #: dot-boundary rule — undeclared hosts still fail closed.
    allowed_domains: frozenset[str] = Field(default_factory=frozenset)


#: Wire status literals (v3 spec §2.1.3/§3.4) — the single source of truth:
#: health.py imports these; no second copy of the strings exists anywhere.
STATUS_OK: Literal["ok"] = "ok"
STATUS_DEGRADED: Literal["degraded"] = "degraded"
STATUS_DOWN: Literal["down"] = "down"
#: Transient "session is warming up" state (issue #193) — reported on
#: /api/providers while uakino's browser session has not become ready.
STATUS_WARMING: Literal["warming"] = "warming"

HealthStatus = Literal["ok", "degraded", "down", "warming"]


class ProviderInfo(BaseModel):
    """One provider's capabilities on ``/api/providers``.

    Model B shape (ADR-0001, contract #135): ``forms`` is the rollup of
    the provider's cinematic-vs-episodic forms, ``styles`` the rollup of
    its style-tagged content (∅ = no style-tagged content). This replaces
    the legacy single ``types`` axis.
    """

    id: str
    name: str
    forms: list[MediaForm]
    styles: list[MediaStyle]
    status: HealthStatus = "ok"
    last_error_at: str | None = None


class Section(BaseModel):
    id: str
    title: str
    # Model B filter axes (ADR-0001, ticket #134, contract #135): the
    # section narrows its browse results by these match rules (CONTEXT.md
    # «Section schema»). The legacy ``type`` axis is gone — a section's
    # kind is exactly its ``form`` + ``styles`` filter axes (∅ = the
    # ordinary-only filter).
    #   form — exact-or-None: ``None`` passes everything, else
    #     ``item.form == section.form`` must hold.
    #   styles — 3-case filter: ``None`` passes anything (including
    #     empty); ``frozenset()`` (∅) passes only ordinary-only items
    #     (``item.styles == frozenset()``); a non-empty set passes iff
    #     ``item.styles & section.styles`` is non-empty (intersection).
    # Optional until the section migration populates them; both default
    # to ``None`` (pass-all) so today's sections behave unchanged.
    form: MediaForm | None = None
    styles: frozenset[MediaStyle] | None = None

    @field_serializer("styles")
    def _ser_styles(self, value: frozenset[MediaStyle] | None) -> list[str] | None:
        # frozenset is not JSON-serializable; emit a stable sorted list.
        # None (pass-any) stays None on the wire to distinguish it from
        # an explicit ∅ (ordinary-only) filter.
        if value is None:
            return None
        return sorted(value)


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
# ``form``/``styles`` and ``poster`` fields are sourced from the
# first-seen provider; the spec doesn't preserve attribution at the
# field level, only at the row level (via ``providers``).
#
# ``HomeRow`` aggregates ``HomeItem`` rows under a human label. The
# ``type`` field doubles as a routing key for the row's contents; every
# valid value is an entry of the declarative table in
# ``cs_uk_api.row_kinds`` (the single source of kind facts — titles,
# filters, wire mappings). Highlights:
#   - ``"recent_movie"``/``"recent_series"`` — «Нещодавно додані»,
#     the providers' newest listings split by form (spec #263).
#   - ``"popular"`` — «Популярні зараз», only when animeon's ``popular``
#     browse returned data (issue #70 AC).
#   - a media-kind literal (``"movie"``, ``"series"``, ``"anime"``,
#     ``"cartoon"``, ``"dorama"``) — one row per kind, aggregating every
#     provider section whose Model B axes (``form``/``styles``) map to
#     that kind (``home.section_row_type``).
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
    poster: str | None = None
    # Model B (contract #135): ``form`` is required, ``styles`` the
    # decided tag set (∅ = ordinary). The legacy ``type`` axis is gone.
    form: MediaForm
    styles: frozenset[MediaStyle] = Field(default_factory=_empty_styles)
    #: Genre labels from the contributing card(s) (ticket #213), unioned
    #: across sources like ``styles`` — first-seen order preserved.
    genres: list[str] = Field(default_factory=list)
    #: Provider ids that contributed this row. Always non-empty (a row
    #: with zero providers was dropped upstream). Order = round-robin
    #: visit order across providers; first-seen wins for the title
    #: fields.
    providers: list[str] = Field(default_factory=list)

    @field_serializer("styles")
    def _ser_styles(self, value: frozenset[MediaStyle]) -> list[str]:
        # frozenset is not JSON-serializable; emit a stable sorted list.
        return sorted(value)
    #: Every per-item group key that contributed to this merged row
    #: (issue #89). The canonical ``group_key`` is the yearful-
    #: preferred-min of those — for a year-soft pair (yearful +
    #: yearless), the min key is the yearful one. A client resume
    #: record keyed by the yearless member would NOT match
    #: ``group_key`` on its own; the client matches against any
    #: element of ``member_keys`` instead. Same content as
    #: ``SearchGroup.member_keys`` because both routes share the
    #: merge core.
    member_keys: list[str] = Field(default_factory=list)


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


class GroupSourceContentResponse(ContentResponse):
    """``/api/content/{groupKey}?source=<provider>`` payload (v3 spec §3.3).

    Inherits every field of ``ContentResponse`` (id, form, title, year,
    description, poster, translations, seasons, translations_level,
    country, group_key) — the chosen source's content body is the
    response body verbatim, no transformation. The added ``sources``
    field is the chip-strip roster: every provider that surfaced this
    group in ``/api/home``, with each provider's content id so the UI
    can switch sources without re-running ``/api/home`` (the spec's
    "Fetching another source = a new request with a spinner" line, §3.3).

    The ``sources`` shape matches the §3.2 grouped-card shape exactly
    (``[{ "provider": ..., "id": ... }]``) so the chip strip and the
    home row's chip strip share one parsing path on the client.

    Issue #60 / v3 spec §3.3.
    """

    #: All providers that surfaced this group in ``/api/home`` (the
    #: chip-strip roster). Order = first-seen by ``/api/home``'s
    #: round-robin walk, matching the home row's ``HomeItem.providers``.
    #: Each entry carries the provider's content id — the same
    #: ``(provider, id)`` pair the home listing surfaced — so the UI
    #: can drive ``/api/content/{groupKey}?source=<p>`` directly from
    #: this echo without a second listings round-trip.
    sources: list[SearchResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM taste-profile value types (spec #290)
#
# The validated v1 profile is a plain frozen dataclass VALUE shared by two
# layers: ``llm.py`` fetches/parses/validates it, and the pure scorer in
# ``recommend.py`` consumes it. The type lives here so the pure core never
# imports the LLM client module (its httpx/config chain) — the dependency
# points one way: llm → models ← recommend. ``llm`` re-exports these names
# for its existing call sites.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowIdea:
    title: str
    genres: tuple[str, ...]
    max: int


@dataclass(frozen=True)
class TasteProfile:
    """The validated LLM taste profile (v1)."""

    genre_weights: dict[str, float] = field(default_factory=dict)
    theme_tags: tuple[str, ...] = ()
    row_ideas: tuple[RowIdea, ...] = ()
