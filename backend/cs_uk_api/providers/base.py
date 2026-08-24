from __future__ import annotations

import abc
import json
import re
from typing import Any, Literal
from urllib.parse import unquote

import httpx

from ..models import (
    ContentResponse,
    Person,
    SearchResult,
    Section,
    StreamResponse,
)
from ..wire_identity import MOVIE_SUFFIX as MOVIE_SUFFIX  # noqa: PLC0414 (re-export, spec #340)

MediaTypeStr = Literal["movie", "series", "anime", "cartoon", "dorama"]


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


class ProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def dle_page_number(href: str) -> int:
    """Pull the page integer out of a pagination link (``/page/N/`` or ``?paged=N``)."""
    m = re.search(r"/page/(\d+)/?", href)
    if m:
        return int(m.group(1))
    m = re.search(r"[?&]paged=(\d+)", href)
    if m:
        return int(m.group(1))
    return 0


def dle_has_next(html: str, page: int) -> bool:
    """Scan pagination blocks for a link beyond ``page``."""
    from bs4 import BeautifulSoup  # local import to avoid cycle

    soup = BeautifulSoup(html, "lxml")
    for a in soup.select(".navigation a[href*='/page/'], div.navigation a[href*='/page/'], div.pages a[href*='/page/'], div.pagination a.page-numbers"):
        href = str(a.get("href") or "")
        if dle_page_number(href) > page:
            return True
    # fallback: any pagination anchor beyond page (WordPress /page/ or ?paged=)
    for a in soup.select("a[href*='/page/'], a[href*='paged=']"):
        href = str(a.get("href") or "")
        if dle_page_number(href) > page:
            return True
    return False


def cards_from(html: str, selector: str, parse_card: Any) -> list[Any]:
    """Loop ``selector`` + ``parse_card`` with None-skip template."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    out: list[Any] = []
    for card in soup.select(selector):
        parsed = parse_card(card)
        if parsed is not None:
            out.append(parsed)
    return out


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
    #: Hosts this provider may FETCH upstream (ADR-0005): its own site,
    #: its API host and the player/CDN pages it resolves mid-flight.
    #: Declared here, enforced centrally by
    #: :func:`cs_uk_api.http_client.provider_safe_get` on every request
    #: AND every redirect hop — so a hostile CMS page cannot point the
    #: backend at an arbitrary host from its LAN position (SSRF). An
    #: adapter that omits a declaration fails closed: the empty default
    #: admits no fetch at all.
    allowed_hosts: frozenset[str] = frozenset()

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

    @staticmethod
    def stream_headers(referer: str) -> dict[str, str]:
        return {"Referer": referer, "User-Agent": "cs-uk-api/1.0"}

    async def get_json(
        self, url: str, http: httpx.AsyncClient, *, headers: dict[str, str] | None = None
    ) -> Any:
        """GET ``url`` and parse JSON body with canonical error codes."""
        from ..http_client import provider_safe_get

        try:
            response = await provider_safe_get(http, self, url, headers=headers)
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code >= 500:
            raise ProviderError("upstream_unreachable", f"status {response.status_code}")
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        try:
            return response.json()
        except json.JSONDecodeError as error:
            raise ProviderError("parse_failed", str(error)) from error

    async def get_html(
        self, url: str, http: httpx.AsyncClient, *, headers: dict[str, str] | None = None
    ) -> str:
        """GET ``url`` and return text body with canonical error codes."""
        from ..http_client import provider_safe_get

        try:
            response = await provider_safe_get(http, self, url, headers=headers)
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code >= 500:
            raise ProviderError("upstream_unreachable", f"status {response.status_code}")
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        return response.text

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
