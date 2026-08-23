"""Wire identity: the id grammar that crosses the API (spec #309, #340).

One module owns every id grammar the codebase used to re-derive by
hand:

- the group-key prefix (``g2:`` — the merged-title identity built by
  ``merge.group_key``) plus the title normalization and per-item key
  derivation it is built from;
- the episode-tail grammar (``:s1e1`` / ``:e5`` / ``:eN:<blob>`` — how
  an episode wire id ends);
- the movie-suffix sentinel (``:__movie__`` — the episode-slot id a
  movie's content page emits so ``stream()`` can tell a film from an
  episode);
- the ``provider:external`` composite split (``split_wire_id``, moved
  here from ``probe`` — this is THE canonical copy).

plus the single projection function for merged groups (canonical fields
+ member keys) that the home rows, the search groups and the group-key
resolution map all used to reproduce four times (US4/US5).

Import direction (#340): strictly one-way, ``merge`` → this module.
``wire_identity`` imports nothing but models + stdlib — never
``merge`` (the guard tests in ``tests/test_wire_identity.py`` enforce
it). The projection's group argument is typed by a structural protocol
so not even a ``TYPE_CHECKING`` import of ``merge.MergeGroup`` remains.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import MediaForm, MediaStyle, SearchResult

#: The versioned prefix of a merged-title group key (``g2:`` = Model B
#: contract step #135 regeneration). A version bump edits ONE file:
#: ``merge.group_key`` builds every key from this constant and
#: ``is_group_key`` recognizes it, so callers never spell ``gN:``
#: themselves.
GROUP_KEY_PREFIX = "g2:"


def group_key(alias: str, form: str, year: int | None) -> str:
    """Stateless versioned group identity (the merge core's key builder)."""
    digest = hashlib.sha1(f"{alias}|{form}|{year or ''}".encode()).hexdigest()
    return f"{GROUP_KEY_PREFIX}{digest[:16]}"


def is_group_key(item_id: str) -> bool:
    """True for a merged ``g2:`` group key (the wire's merged-title id).

    The shape that distinguishes a group key from an episode wire id or
    a view id on the wire, so callers can route without re-deriving the
    prefix.
    """
    return item_id.startswith(GROUP_KEY_PREFIX)


# ---------------------------------------------------------------------------
# Title normalization — the pure primitives the per-item key derivation
# composes (moved here from ``merge`` so the module stays a leaf, #340).
# ---------------------------------------------------------------------------

#: DLE-style sites sprinkle codec/quality labels into titles.
_QUALITY_TAGS = re.compile(
    r"\b(?:480p|576p|720p|1080p|2160p|4320p|4k|8k|uhd|fhd|hdr(?:10)?|"
    r"web[- ]?dl|web[- ]?rip|bd[- ]?rip|dvd[- ]?rip|dvdscr|hdtv|"
    r"cam(?:rip)?|ts|x26[45]|h26[45]|hevc|avc|aac|ac3|dts|mp3|"
    r"укр|ukr|ua|дубляж|субтитри)\b",
    re.IGNORECASE,
)

#: A bracketed year anywhere: "(2021)", "[2009]".
_BRACKET_YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
#: A bare trailing year token: "Дюна 2021".
_TRAILING_YEAR = re.compile(r"\s((?:19|20)\d{2})\s*$")

#: Trailing content-type words tailing a title: "Дюна (фільм)", "Смолфут мультфільм".
_TAIL_TYPES = ("фільм", "серіал", "мультфільм", "мультсеріал", "аніме", "дорама", "кино", "сериал", "мультик")
_BRACKET_TAIL_TYPE = re.compile(r"[\(\[](?:фільм|серіал|мультфільм|мультсеріал|аніме|дорама)[\)\]]", re.IGNORECASE)

#: Apostrophe variants are DELETED, not normalized to one char: that unifies
#: all of «п'ять», «п’ять», «пять» in one stroke.
_APOSTROPHES = str.maketrans({"’": "", "‘": "", "`": "", "´": "", "ʼ": "", "'": "", '"': ""})

#: " / " separates alternative titles on DLE sites ("Тато / Daddy").
_ALIAS_SPLIT = re.compile(r"\s+/\s+")


def extract_year(raw: str) -> int | None:
    """Extract a 4-digit release year: bracketed anywhere, else bare-trailing."""
    m = _BRACKET_YEAR.search(raw)
    if m is not None:
        return int(m.group(1))
    m = _TRAILING_YEAR.search(raw)
    return int(m.group(1)) if m is not None else None


def _normalize_one(piece: str) -> str:
    s = piece.translate(_APOSTROPHES).lower()
    s = _BRACKET_YEAR.sub(" ", s)
    s = _TRAILING_YEAR.sub("", s)
    s = _QUALITY_TAGS.sub(" ", s)
    s = _BRACKET_TAIL_TYPE.sub(" ", s)
    # Punctuation and symbols become spaces (.,:;!?()[]{}«»"„“–—-/ etc.).
    s = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in s)
    tokens = s.split()
    while tokens and tokens[-1] in _TAIL_TYPES:
        tokens.pop()
    return " ".join(tokens)


def title_aliases(raw: str) -> tuple[str, ...]:
    """Normalized alternative titles, first-normalized-first, deduped."""
    seen: list[str] = []
    for piece in _ALIAS_SPLIT.split(raw.strip()):
        norm = _normalize_one(piece)
        if norm and norm not in seen:
            seen.append(norm)
    return tuple(seen)


def normalize_title(raw: str) -> str:
    """Canonical normalized form of a title (its primary alias)."""
    aliases = title_aliases(raw)
    return aliases[0] if aliases else ""


def effective_year(title: str, year: int | None) -> int | None:
    """A title's year: the explicit field, else one parsed from the raw title."""
    return year if year is not None else extract_year(title)


def group_key_from(title: str, form: str, year: int | None, item_id: str) -> str:
    """Stateless group identity for one raw title (issue #69, v3 spec §4.3).

    A pure function of the item's own listing data — its most canonical
    alias, its ``form`` (movie/series), its RAW year field — so the same
    title always yields
    the same key no matter which other providers appear in the same call.
    The raw year is composed through ``effective_year`` (explicit year,
    else title-parsed) internally, exactly as the merge core composes it
    for matching, so a caller passing the raw field always agrees with
    ``merge.merge_results``.
    """
    aliases = title_aliases(title)
    key_alias = min(aliases) if aliases else f"id:{item_id}"
    return group_key(key_alias, form, effective_year(title, year))


def item_group_key(it: SearchResult) -> str:
    """Stateless per-item group identity: ``group_key_from`` over the item.

    Public seam for callers that hold one item (e.g. ``/api/content``) and
    need the same key the merge core would produce for it. The item's raw
    year field is passed through — ``group_key_from`` applies the effective
    year itself. The identity axis is ``form`` (contract #135) — ``type``
    no longer exists on items.
    """
    return group_key_from(it.title, it.form, it.year, it.id)


# ---------------------------------------------------------------------------
# Episode-tail grammar
# ---------------------------------------------------------------------------

#: Episode wire ids end in ``:s1e1`` (ufdub-style) or ``:e5``
#: (uakino/kinotron-style), or carry a base64 source blob AFTER the
#: ``:eN`` tail (animeon-style). The tail is ``:e<N>`` (optionally with
#: a season prefix) followed by ``:`` or end-of-string, never digits.
EPISODE_TAIL_RE = re.compile(r":(?:s\d+)?e\d+(?=:|$)")


def split_episode_tail(item_id: str) -> tuple[str, str] | None:
    """Split an episode wire id at its tail: ``(composite, tail)`` or None.

    ``ufdub:dorama-408-...:s1e1`` → ``(``ufdub:dorama-408-...``,
    ``:s1e1``)``; ``animeon:918:e1:eyJ...`` → ``(``animeon:918``,
    ``:e1:eyJ...``)``. A non-episode id (a ``g2:`` group key, a movie
    suffix id, a plain composite) → None. The ``provider:external``
    composite before the tail is what identifies the merged group
    (reverse lookup, #214).
    """
    match = EPISODE_TAIL_RE.search(item_id)
    if match is None:
        return None
    return item_id[: match.start()], item_id[match.start() :]


#: The season/episode tail shape the ``s{N}e{M}`` builders emit and the
#: adapters' stream() paths parse (#346). ``parse_episode_tail`` accepts
#: the tail with or without its leading colon so both the raw id suffix
#: and a :func:`split_episode_tail` result feed it unchanged.
SEASON_EPISODE_TAIL_RE = re.compile(r"s(\d+)e(\d+)$")


def parse_episode_tail(tail: str) -> tuple[int, int] | None:
    """Parse an ``s<N>e<M>`` episode tail -> ``(season, episode)`` or None.

    Accepts ``":s2e10"`` (a :func:`split_episode_tail` tail) or
    ``"s2e10"`` (the bare rpartition suffix most adapters hold). Anything
    else — empty, bare-``:eN``, malformed — is None; the caller surfaces
    its own error.
    """
    match = SEASON_EPISODE_TAIL_RE.fullmatch(tail.lstrip(":"))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def episode_wire_id(provider: str, external: str, season: int, episode: int) -> str:
    """Build the series-episode wire id ``<provider>:<external>:s<N>e<M>``.

    The single builder for the ``:s{N}e{M}`` tail (#346) — every adapter
    emits episode ids through this instead of hand-formatting the
    grammar.
    """
    return f"{provider}:{external}:s{season}e{episode}"


# ---------------------------------------------------------------------------
# Movie-suffix sentinel
# ---------------------------------------------------------------------------

#: The episode-slot id suffix a movie's content page emits (defined 9
#: times across the adapters before spec #309; now one definition).
#: ``stream()`` distinguishes a film (``<external>:__movie__``) from an
#: episode (``<external>:s1e1`` / ``:e5``) by this sentinel.
MOVIE_SUFFIX = ":__movie__"


def is_movie_wire_id(item_id: str) -> bool:
    """True when the id carries the movie-suffix sentinel."""
    return item_id.endswith(MOVIE_SUFFIX)


def strip_movie_suffix(item_id: str) -> str:
    """The bare external id of a movie wire id (suffix removed, if any).

    ``<external>:__movie__`` → ``<external>``; an id without the suffix
    passes through unchanged (the bare-id movie form some clients send).
    """
    if item_id.endswith(MOVIE_SUFFIX):
        return item_id[: -len(MOVIE_SUFFIX)]
    return item_id


# ---------------------------------------------------------------------------
# Composite split — THE canonical provider:external split (moved from
# ``probe`` where it was already declared canonical, #340).
# ---------------------------------------------------------------------------


def split_wire_id(composite: str) -> tuple[str, str]:
    """``provider:external`` -> ``(provider, external)`` — the canonical split.

    Splits on the FIRST colon only: external ids may legitimately carry
    colons (episode wire ids like ``uakino:6268:e1`` or
    ``ufdub:dorama-408-123:s1e1``). A composite without a colon yields an
    empty external (``("x", "")``) — callers that require a valid
    provider prefix validate that themselves.
    """
    provider, _, external = composite.partition(":")
    return provider, external


# ---------------------------------------------------------------------------
# Single merge projection
# ---------------------------------------------------------------------------


class MergedGroupLike(Protocol):
    """Structural view of ``merge.MergeGroup`` for :func:`project_group`.

    Keeps the merge → wire_identity edge strictly one-way (#340): the
    projection reads only ``key`` + ``sources``, so any group shape
    with those members conforms — no ``merge`` import needed, not even
    under ``TYPE_CHECKING``. Read-only properties so the frozen
    dataclass conforms.
    """

    @property
    def key(self) -> str: ...

    @property
    def sources(self) -> tuple[SearchResult, ...]: ...


@dataclass(frozen=True)
class GroupProjection:
    """Canonical fields + member keys of one merged group (spec #309).

    What the home rows, the search groups and the group-key resolution
    map all used to re-derive by hand: the canonical fields come from
    the first-seen source, ``member_keys`` are the deduped per-item
    group keys of every source, ``providers`` the first-seen provider
    union. Wire-visible semantics are unchanged — this is the single
    place the projection rules live (US5).
    """

    key: str
    title: str
    year: int | None
    poster: str | None
    form: MediaForm
    styles: frozenset[MediaStyle]
    genres: tuple[str, ...]
    providers: tuple[str, ...]
    sources: tuple[SearchResult, ...]
    member_keys: tuple[str, ...]


def project_group(mg: MergedGroupLike) -> GroupProjection:
    """Project one merged group to its canonical fields + member keys.

    First-seen wins every canonical field (the merge core preserves
    bucket order = first-seen order); ``member_keys`` is the deduped,
    first-seen-preserved list of the sources' per-item group keys
    (issue #89 — the client matches a resume record against ANY member
    key, not only the canonical ``group_key``).
    """
    sample = mg.sources[0]
    return GroupProjection(
        key=mg.key,
        title=sample.title,
        year=sample.year,
        poster=sample.poster,
        form=sample.form,
        styles=sample.styles,
        genres=tuple(sample.genres),
        providers=tuple(dict.fromkeys(s.provider for s in mg.sources)),
        sources=mg.sources,
        member_keys=tuple(dict.fromkeys(item_group_key(s) for s in mg.sources)),
    )


def provider_union(sources: Sequence[SearchResult]) -> dict[str, SearchResult]:
    """``provider -> first-seen SearchResult`` over a group's sources.

    The resolution map's shape (``group_key -> {provider: SearchResult}``,
    ticket #101): first-seen wins, so the provider order matches the
    chip strip the home rows surface.
    """
    union: dict[str, SearchResult] = {}
    for s in sources:
        union.setdefault(s.provider, s)
    return union
