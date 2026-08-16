"""Model B catalog filter predicates (ADR-0001, ticket #134).

Pure functions — no I/O, no routing. The ``/api/search`` axis filters
and the ``/api/browse`` section match semantics lived in the route
module (main.py) and were only testable through the whole FastAPI app;
they belong here so they're unit-testable in isolation.

Two callers share them:

  - ``/api/search`` uses ``parse_style_filter`` / ``style_key`` /
    ``matches_axes`` for the ``?form=`` / ``?style=`` query axes.
  - ``/api/browse`` uses ``section_matches`` for the section's declared
    ``form`` / ``styles`` match semantics (CONTEXT.md «Section schema»).

None of these read settings, so moving them out of the route module does
not change the ``config.SETTINGS`` test patch point (Arch T12).
"""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException

from .models import ErrorResponse, MediaForm, MediaStyle, SearchResult, Section

#: Valid ``?style=`` tokens (Model B, ticket #134). Kept as a module
#: constant because ``MediaStyle.__args__`` is a typing special form
#: that mypy rejects on attribute access.
STYLE_TOKENS: frozenset[str] = frozenset({"anime", "cartoon", "dorama"})
#: Valid ``?form=`` tokens. ``form`` is a single-token axis (exact-or-None
#: filter), unlike the comma-list ``style`` axis.
FORM_TOKENS: frozenset[str] = frozenset({"movie", "series"})


def section_matches(item: SearchResult, section: Section) -> bool:
    """Model B section match semantics (CONTEXT.md «Section schema»).

    - ``form``: ``None`` passes everything; else ``item.form ==
      section.form`` must hold.
    - ``styles``: 3-case — ``None`` passes anything (including empty);
      ``frozenset()`` (∅) passes only ordinary-only items
      (``item.styles == frozenset()``); a non-empty set passes iff
      ``item.styles & section.styles`` is non-empty (intersection).
    """
    if section.form is not None and item.form != section.form:
        return False
    if section.styles is None:
        return True
    if not section.styles:
        return not item.styles
    return bool(item.styles & section.styles)


def parse_style_filter(raw: str | None) -> frozenset[MediaStyle] | None:
    """Parse the ``?style=`` query param into a style filter set.

    Decided semantics (CONTEXT.md «Search filter axes»): a comma-
    separated list with intersection matching — an item passes iff it
    carries at least one of the requested styles. Absent/empty = any
    (``None``). There is deliberately NO ordinary-only token on search:
    ``Section`` is the way to filter to ordinary-only (∅ styles), and
    ``?style`` stays a plain intersection list.

    Invalid style tokens raise a 400 — a typo should surface at the
    API boundary, not silently pass everything.
    """
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    invalid = [p for p in parts if p not in STYLE_TOKENS]
    if invalid:
        raise HTTPException(
            400,
            detail=ErrorResponse(
                error="invalid_style",
                message=f"unknown style token(s): {', '.join(invalid)}",
            ).model_dump(),
        )
    return frozenset(cast(MediaStyle, p) for p in parts)


def parse_form_filter(raw: str | None) -> MediaForm | None:
    """Parse the ``?form=`` query param into a form filter value.

    Mirrors ``parse_style_filter`` (ticket #141): a single token,
    exact-or-None match (``None`` = any). A typo must surface as the
    same custom 400 envelope as a bad style token — ``invalid_form`` —
    instead of FastAPI's default 422 for the old ``Literal`` query
    param, so the client parses both axes identically.
    """
    if raw is None or not raw.strip():
        return None
    token = raw.strip()
    if token not in FORM_TOKENS:
        raise HTTPException(
            400,
            detail=ErrorResponse(
                error="invalid_form",
                message=f"unknown form token: {token}",
            ).model_dump(),
        )
    return cast(MediaForm, token)


def style_key(style_filter: frozenset[MediaStyle] | None) -> str:
    """Stable cache-key fragment for a style filter."""
    if not style_filter:
        return ""
    return ",".join(sorted(style_filter))


def matches_axes(
    item: SearchResult,
    form: MediaForm | None,
    style_filter: frozenset[MediaStyle] | None,
) -> bool:
    """Model B axis match for a single search result (ADR-0001).

    - ``form``: exact-or-None — ``None`` passes, else
      ``item.form == form`` must hold. An item whose provider hasn't
      populated ``form`` yet (``None``) fails an explicit filter.
    - ``style_filter``: ``None`` passes; a non-empty set passes iff
      ``item.styles & style_filter`` is non-empty (intersection).
    """
    return (form is None or item.form == form) and (
        style_filter is None or bool(item.styles & style_filter)
    )


__all__ = [
    "FORM_TOKENS",
    "STYLE_TOKENS",
    "matches_axes",
    "parse_form_filter",
    "parse_style_filter",
    "section_matches",
    "style_key",
]
