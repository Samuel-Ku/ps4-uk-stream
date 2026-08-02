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


@dataclass(frozen=True)
class MergeGroup:
    """One merged title: a stable key, a display representative, all sources."""

    key: str
    representative: SearchResult
    sources: tuple[SearchResult, ...] = field(compare=False)


def _effective_year(it: SearchResult) -> int | None:
    return it.year if it.year is not None else extract_year(it.title)


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
        first = items[members[0]]
        all_aliases = {a for m in members for a in per_item_aliases[m]}
        if all_aliases:
            canonical = min(all_aliases)
        else:
            # Title that normalized to nothing (e.g. "(2021)") can never
            # merge; anchor its key to the item id so it stays a stable
            # singleton.
            canonical = f"id:{first.id}"
        years = {y for m in members if (y := _effective_year(items[m])) is not None}
        key_year = years.pop() if len(years) == 1 else None
        groups.append(
            MergeGroup(
                key=group_key(canonical, first.type, key_year),
                representative=first,
                sources=tuple(items[m] for m in members),
            )
        )
    return groups
