from __future__ import annotations

import abc
import re
from typing import Any, Literal
from urllib.parse import unquote

import httpx

from ..models import (
    ContentResponse,
    MediaForm,
    MediaStyle,
    Person,
    SearchResult,
    Section,
    StreamResponse,
)

MediaTypeStr = Literal["movie", "series", "anime", "cartoon", "dorama"]

#: Style-tagged MediaType values map 1:1 to a MediaStyle.
_STYLE_BY_TYPE: dict[str, MediaStyle] = {
    "anime": "anime",
    "cartoon": "cartoon",
    "dorama": "dorama",
}


def parse_actor_list(
    soup: Any,
    label: str,
    provider: str,
    href_re: re.Pattern[str],
) -> list[Person]:
    """Parse a ``<li><span>Label:</span> <a href=…>Name</a>, …</li>``
    cast row into Person entries (ticket #221).

    The DLE-family sites that expose cast share this block shape —
    kinotron's ``В ролях:`` and uaserialspro's ``Актори:`` — with one
    anchor per person whose href carries the stable person key.
    ``href_re`` extracts that key from the href (the first capture
    group); the display name is the fallback key when the link has no
    matching shape. Returns [] when the page has no such row.
    """
    for li in soup.select("li"):
        span = li.select_one("span")
        if span is None or label not in span.get_text():
            continue
        people: list[Person] = []
        for a in li.select("a"):
            name = a.get_text(strip=True)
            if not name:
                continue
            m = href_re.search(str(a.get("href") or ""))
            key = unquote(m.group(1)) if m else name
            people.append(Person(id=f"{provider}:{key}", name=name))
        return people
    return []


def model_b_axes(
    media_type: MediaTypeStr,
    *,
    form: MediaForm | None = None,
) -> tuple[MediaForm, frozenset[MediaStyle]]:
    """Map a legacy ``MediaType`` to Model B axes (ADR-0001, expand
    step #129).

    ``movie``/``series`` map directly with an empty style set (ordinary
    live-action). Style-tagged types (``anime``/``cartoon``/``dorama``)
    always carry their style; their ``form`` defaults to ``"series"``
    unless the caller knows the item is a film and passes ``form``
    explicitly (e.g. an anime movie in a ``films`` section).
    """
    if media_type in ("movie", "series"):
        return media_type, frozenset()
    style = _STYLE_BY_TYPE[media_type]
    return (form or "series"), frozenset({style})


class ProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BaseProvider(abc.ABC):
    id: str
    name: str
    types: tuple[MediaTypeStr, ...]
    # Subclasses set this to a non-empty tuple to opt into /api/sections
    # and /api/browse. Default: no section browsing.
    sections: tuple[Section, ...] = ()
    #: Subclasses set this to a section id whose first page is the
    #: provider's "newest releases" listing (issue #70, «Новинки» row).
    #: When ``None`` (the default), the provider contributes nothing to
    #: «Новинки»; only providers with an explicit newest section
    #: (e.g. animeon's ``"page"``) round-robin into the merged list.
    newest_section: str | None = None
    #: Providers that can serve subscription-gated placeholder streams
    #: (a "Для підписників" sponsor clip instead of the real video —
    #: e.g. BambooUA's ``be_sponsors.mp4``) set this True. The catalog
    #: build then resolves their cards and drops gated ones before
    #: merging rows, so a promo clip never surfaces as a playable card.
    can_gate: bool = False

    def has_section(self, section_id: str) -> bool:
        return any(s.id == section_id for s in self.sections)

    @abc.abstractmethod
    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]: ...

    @abc.abstractmethod
    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse: ...

    @abc.abstractmethod
    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse: ...

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        """Return one page of results for a section.

        Default implementation: raises NotImplementedError. Providers that
        declare `sections` must override this.
        """
        raise NotImplementedError(f"{self.id} does not support browse")

    async def episode_translations(
        self, content_id: str, http: httpx.AsyncClient
    ) -> list[str] | None:
        """Return allowed translation ids for an episode, or None.

        Providers that support per-episode translations (translations_level
        == "episode") override this. Returning None means "not applicable"
        — the caller falls back to content-level translations. Returning a
        list (possibly empty) means the caller should validate against it.
        """
        return None
