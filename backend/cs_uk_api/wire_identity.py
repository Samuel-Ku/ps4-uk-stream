"""Wire identity: the id grammar that crosses the API (spec #309, step 1).

One module owns the three id grammars the codebase used to re-derive by
hand:

- the group-key prefix (``g2:`` — the merged-title identity built by
  ``merge.group_key``);
- the episode-tail grammar (``:s1e1`` / ``:e5`` / ``:eN:<blob>`` — how
  an episode wire id ends);
- the movie-suffix sentinel (``:__movie__`` — the episode-slot id a
  movie's content page emits so ``stream()`` can tell a film from an
  episode).

plus the single projection function for merged groups (canonical fields
+ member keys) that the home rows, the search groups and the group-key
resolution map all used to reproduce four times (US4/US5).

Import cycle note: ``merge`` imports ``GROUP_KEY_PREFIX`` from here, so
this module must NOT import ``merge`` at module level — ``project_group``
imports ``item_group_key`` lazily inside the function body (the same
cycle-break pattern ``poster_proxy`` uses for the uakino browser
session). Everything else imports at the top.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import MediaForm, MediaStyle, SearchResult

if TYPE_CHECKING:
    from .merge import MergeGroup

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


def project_group(mg: MergeGroup) -> GroupProjection:
    """Project one merged group to its canonical fields + member keys.

    First-seen wins every canonical field (the merge core preserves
    bucket order = first-seen order); ``member_keys`` is the deduped,
    first-seen-preserved list of the sources' per-item group keys
    (issue #89 — the client matches a resume record against ANY member
    key, not only the canonical ``group_key``).
    """
    from .merge import item_group_key  # cycle-break, see module docstring

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
