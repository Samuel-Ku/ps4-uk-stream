"""Cross-provider merge core (issue #52, v3 spec §4).

Pure functions, no I/O. Two listing items from different providers are the
same title iff:

    normalized title (any alias) equal
    AND type equal
    AND (year equal OR at least one year unknown)

Group keys are stateless and versioned: ``group_key`` = "g1:" + sha1 of the
canonical (alias|type|year) triple, so a normalization-rule change is a
simple "gN:" bump, never a data migration.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import SearchResult

log = logging.getLogger("cs_uk_api.merge")

_KEY_VERSION = "g1"

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


def group_key(alias: str, media_type: str, year: int | None) -> str:
    """Stateless versioned group identity."""
    digest = hashlib.sha1(f"{alias}|{media_type}|{year or ''}".encode()).hexdigest()
    return f"{_KEY_VERSION}:{digest[:16]}"


def effective_year(title: str, year: int | None) -> int | None:
    """A title's year: the explicit field, else one parsed from the raw title."""
    return year if year is not None else extract_year(title)


def group_key_from(title: str, media_type: str, year: int | None, item_id: str) -> str:
    """Stateless group identity for one raw title (issue #69, v3 spec §4.3).

    A pure function of the item's own listing data — its most canonical
    alias, its type, its RAW year field — so the same title always yields
    the same key no matter which other providers appear in the same call.
    The raw year is composed through ``effective_year`` (explicit year,
    else title-parsed) internally, exactly as the merge core composes it
    for matching, so a caller passing the raw field always agrees with
    ``merge_results``.
    """
    aliases = title_aliases(title)
    key_alias = min(aliases) if aliases else f"id:{item_id}"
    return group_key(key_alias, media_type, effective_year(title, year))


@dataclass(frozen=True)
class MergeGroup:
    """One merged title: a stable key plus all its source items."""

    key: str
    sources: tuple[SearchResult, ...] = field(compare=False)


def _effective_year(it: SearchResult) -> int | None:
    return effective_year(it.title, it.year)


def item_group_key(it: SearchResult) -> str:
    """Stateless per-item group identity: ``group_key_from`` over the item.

    Public seam for callers that hold one item (e.g. ``/api/content``) and
    need the same key the merge core would produce for it. The item's raw
    year field is passed through — ``group_key_from`` applies the effective
    year itself.
    """
    return group_key_from(it.title, it.type, it.year, it.id)


def _years_match(a: int | None, b: int | None) -> bool:
    return a is None or b is None or a == b


def merge_results(items: Iterable[SearchResult]) -> list[MergeGroup]:
    """Union-find merge of listing items by alias+type with the year-soft rule.

    Every actual union of two previously-separate items is INFO-logged with
    both raw titles — the only after-the-fact wrong-merge detector.
    """
    items = list(items)
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[ra] = rb
        log.info(
            "merge: '%s'[%s] + '%s'[%s]",
            items[a].title, items[a].id,
            items[b].title, items[b].id,
        )
        return True

    seen: dict[tuple[str, str], list[int]] = {}
    per_item_aliases = [title_aliases(it.title) for it in items]
    for i, it in enumerate(items):
        for alias in per_item_aliases[i]:
            for j in seen.get((alias, it.type), []):
                if _years_match(_effective_year(it), _effective_year(items[j])):
                    union(i, j)
            seen.setdefault((alias, it.type), []).append(i)

    buckets: dict[int, list[int]] = {}
    for i in range(len(items)):
        buckets.setdefault(find(i), []).append(i)

    groups: list[MergeGroup] = []
    for members in buckets.values():
        # Group key = min over the members' own stateless item keys:
        # order-independent, and every member can recompute its own key
        # from its listing data alone (issue #69, v3 spec §4.3). Members
        # with a known year are preferred: otherwise two different
        # year-soft groups (Дюна 2021 + yearless vs Дюна 1984 + yearless)
        # would both min to the yearless member's key (its digest hashes
        # below both year variants) and collide, silently dropping one
        # title at groupKey-dedup. Yearless singletons keep their own key.
        yearful = [m for m in members if _effective_year(items[m]) is not None]
        preferred = yearful if yearful else members
        key = min(item_group_key(items[m]) for m in preferred)
        groups.append(
            MergeGroup(
                key=key,
                sources=tuple(items[m] for m in members),
            )
        )
    return groups
