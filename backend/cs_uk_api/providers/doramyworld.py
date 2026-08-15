"""DoramyWorld provider (https://doramy.world) — Ukrainian-dubbed doramas
(Korean/Japanese/Chinese), entertainment shows and Asian films.
Issue #17, Group 2.

The upstream Kotlin parses a JSON block from the
``.external-video-player-holder[data-player]`` attribute for the season/
episode manifest and walks the iframe to ``ashdi.vip`` for the .m3u8. We
mirror the same data shape as Pydantic DTOs (``_PlayerTranslation`` /
``_PlayerSeason``) so a future upstream rename does not silently break
parsing.

Card markup is WordPress-native; the listings live at ``/{film,dorama,
show}/page/N/`` (the upstream's pretty-permalink form).
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ValidationError

from ..country import extract_country
from ..extractors import RegexExtractor
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import (
    BaseProvider,
    ProviderError,
    ProviderErrorCode,
    model_b_axes,
    split_content_suffix,
)

BASE_URL = "https://doramy.world"
# ashdi.vip hosts the HLS manifest for each episode; the upstream Kotlin
# uses the same Referer.
ASHDI_REFERER = "https://ashdi.vip/"
# Sections exposed by DoramyWorld's main navigation. Per the upstream
# `mainPage = mainPageOf(...)` declaration in DoramyWorldProvider.kt.
DORAMYWORLD_SECTIONS: tuple[Section, ...] = (
    Section(id="film", title="Фільми", form="movie"),
    Section(id="dorama", title="Дорами", styles=frozenset({"dorama"})),
    Section(id="show", title="Розважальні шоу", form="series"),
)

# URL path segment -> MediaType. Longest prefixes first; we only need
# the three the site actually exposes, but the order also future-proofs
# against any nested path. The upstream maps `/film/` -> Movie and
# everything else -> AsianDrama; we map `/dorama/` to its own `dorama`
# MediaType so the front-end can branch on it.
_PATH_TYPE: tuple[tuple[str, str], ...] = (
    ("film", "movie"),
    ("dorama", "dorama"),
    ("show", "series"),
)

# Cards on listings: article.type-{film,dorama,show}. WordPress sets
# both the post class and the post-type taxonomy, so we match the
# class only (matches the upstream Kotlin selector exactly).
_CARD_SELECTOR = "article.type-dorama, article.type-film, article.type-show"

# DLE / WordPress pagination is `<a class="page-numbers"
# href=".../page/N/">` siblings. We additionally accept the query-string
# form `?paged=N` so the helper survives URL rewrites.
_PAGE_RE = re.compile(r"/page/(\d+)/?|\bpaged=(\d+)")

# Whitelisted slug: `kind/name` where kind is film/dorama/show and name
# is a kebab-case slug. Used to validate external_id at the provider
# boundary so callers cannot inject path traversal.
_EXTERNAL_ID_RE = re.compile(r"(film|dorama|show)/[a-z0-9][a-z0-9-]*")

# Episode-id suffix grammar: `s<N>e<M>` (1-based). Matched directly
# against the bare suffix (without the leading ':').
_EP_SUFFIX_RE = re.compile(r"s(\d+)e(\d+)$")


# --- JSON DTOs mirroring the upstream data-player payload --------------------


class _PlayerSeason(BaseModel):
    label: str | None = None
    episodes: list[str] = []


class _PlayerTranslation(BaseModel):
    label: str = ""
    seasons: list[_PlayerSeason] = []


def _parse_player(html: str) -> list[_PlayerTranslation]:
    """Decode the ``data-player`` attribute on the iframe holder.

    WordPress stores it as ``&quot;``-escaped JSON; we unescape first,
    then validate against ``_PlayerTranslation``.
    """
    m = re.search(r'data-player="([^"]*)"', html)
    if not m:
        return []
    try:
        raw = json.loads(unescape(m.group(1)))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[_PlayerTranslation] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(_PlayerTranslation.model_validate(item))
        except ValidationError:
            continue
    return out


def _translation_id(label: str) -> str:
    """Stable translation id derived from the label.

    The data-player label is human-readable Ukrainian (e.g. "K'Di
    (одноголосе озвучення)" or empty for films). We strip the
    parenthetical tail (which describes the dubbing type), lowercase,
    and collapse non-alphanumerics into a single dash. Falling back to
    "uk" keeps the API contract: translations must carry at least one
    entry, and Ukrainian is the default.
    """
    if not label:
        return "uk"
    # Drop everything from the first '(' onward -- the upstream
    # parenthetical is a description of the dubbing type, not part of
    # the studio name we want to surface as an id.
    head = label.split("(", 1)[0].strip().lower()
    if not head:
        return "uk"
    collapsed = re.sub(r"[^a-z0-9]+", "-", head).strip("-")
    return collapsed or "uk"


# --- URL helpers ----------------------------------------------------------------


def _external_id_from_url(href: str) -> str:
    """Return ``kind/slug`` for any URL whose path is ``/{kind}/{slug}/``.

    The upstream site only exposes /film/, /dorama/ and /show/. We
    raise ``parse_failed`` (caller turns into not_found for bad
    content_id) so callers cannot smuggle arbitrary paths through
    the API."""
    m = re.search(r"/(film|dorama|show)/([a-z0-9][a-z0-9-]*)/?", href)
    if not m:
        raise ProviderError(ProviderErrorCode.PARSE_FAILED, f"unrecognized url: {href}")
    return f"{m.group(1)}/{m.group(2)}"


def _type_from_url(href: str) -> str:
    """Map the URL's path segment to a MediaType. Falls back to
    'series' for any URL we don't recognise so the safe default
    mirrors the upstream's else-branch."""
    lower = href.lower()
    for needle, t in _PATH_TYPE:
        if f"/{needle}/" in lower:
            return t
    return "series"


def _page_number(href: str) -> int:
    """Pull the page index out of either `/page/N/` or `?paged=N`."""
    m = _PAGE_RE.search(href)
    if not m:
        return 0
    return int(next(g for g in m.groups() if g is not None))


def _section_url(section: str, page: int) -> str:
    paths = {s.id: f"/{s.id}/" for s in DORAMYWORLD_SECTIONS}
    if section not in paths:
        raise ProviderError(ProviderErrorCode.NOT_FOUND, f"unknown section: {section}")
    # The upstream Kotlin always appends /page/N/, even for page 1.
    # WordPress 301-redirects `/page/1/` to the canonical section URL
    # (`/film/page/1/` -> `/film/`); browse() fetches through guarded_get
    # (#171) so the same-host redirect is followed. Pages > 1 return 200.
    return f"{BASE_URL}{paths[section]}page/{page}/"


# --- Content-page helpers --------------------------------------------------------


def _extract_year(soup: BeautifulSoup) -> int | None:
    """The ``Рік:`` row has the format `<a href="/date/YYYY/">YYYY</a>`.
    We return the first year found; the upstream picks the first match."""
    for li in soup.select("li.item"):
        title = li.select_one("span.title")
        if title is None or "Рік" not in title.get_text():
            continue
        for a in li.select("ul.tax-list a"):
            text = a.get_text(strip=True)
            if text.isdigit() and len(text) == 4:
                return int(text)
    return None


def _extract_description(soup: BeautifulSoup) -> str:
    """Join the paragraphs of `div.about-text` into a single string."""
    holder = soup.select_one("div.about-text")
    if holder is None:
        return ""
    return "\n".join(p.get_text(" ", strip=True) for p in holder.select("p")).strip()


# --- Search-result parsing -------------------------------------------------------


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one WordPress listing card. Cards without a URL or title
    are filtered out by the selector upstream."""
    a = card.select_one("h3.post-title a")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    # The h3 has `<span>Українська</span> <span>English</span>` -- the
    # primary title is the first <span>.
    title_el = a.select_one("span")
    title = (title_el.get_text(strip=True) if title_el else a.get_text(strip=True))
    img = card.select_one(".post-thumbnail img")
    poster_src = str(img["src"]) if img and img.get("src") else None
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    try:
        ext = _external_id_from_url(href)
    except ProviderError:
        return None
    mb_form, mb_styles = model_b_axes(_type_from_url(href))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{ext}",
        provider=provider_id,
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


# --- Provider --------------------------------------------------------------------


class DoramyWorldProvider(BaseProvider):
    id = "doramyworld"
    name = "DoramyWorld"
    types = ("movie", "series", "dorama")
    sections = DORAMYWORLD_SECTIONS
    #: Issue #188: a content page without a player (no ``data-player``
    #: at all) is an unplayable dead card — content() raises ``gated``
    #: and the catalog sweep (``filter_gated_items``) drops it from
    #: home/search instead of surfacing an unplayable movie.
    can_gate = True
    #: SSRF allowlist: the WordPress CMS and the ashdi player CDN. A
    #: hostile CMS response must not be able to pivot the player hop to
    #: an attacker-controlled host.
    hosts: frozenset[str] = frozenset({"doramy.world", "ashdi.vip"})

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # WordPress search uses `?s=...` with spaces encoded as `+`. We
        # hand-roll the URL instead of `quote()` to match the upstream
        # Kotlin `query.replace(" ", "+")` exactly.
        url = f"{BASE_URL}/?s={query.replace(' ', '+')}"
        try:
            resp = await self.guarded_get(http, url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.UPSTREAM_UNREACHABLE, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select(_CARD_SELECTOR):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        url = _section_url(section, page)
        try:
            resp = await self.guarded_get(http, url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select(_CARD_SELECTOR):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # WordPress pagination: `<div class="pagination">` with
        # `<a class="page-numbers" href=".../page/N/">` siblings. Any
        # link to a page higher than `page` means there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("div.pagination a.page-numbers")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"bad external_id: {external_id!r}")
        url = f"{BASE_URL}/{external_id}/"
        try:
            resp = await self.guarded_get(http, url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one("h1.project-title")
        if title_el is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "title missing")
        # The h1 may contain a `<span class="project-title-eng"> /
        # English</span>` tail; `get_text(strip=True)` collapses both.
        title = title_el.get_text(" ", strip=True)
        # Drop a leading separator (`Пан королева / Mr. Queen`) so the
        # title is just the primary Ukrainian name.
        title = title.split("/")[0].strip()
        og = soup.select_one('meta[property="og:image"]')
        poster = urljoin(BASE_URL, str(og["content"])) if og and og.get("content") else None
        year = _extract_year(soup)
        description = _extract_description(soup)
        country: str | None = extract_country(soup)
        media_type = _type_from_url(url)
        translations_models = _parse_player(resp.text)
        if not translations_models:
            # Issue #188: a page without a ``data-player`` has no
            # playable source at all (observed live on «У шкірі моєї
            # матері» — no data-player, no player iframe). Surface the
            # dead card as ``gated`` (ADR-0002) so the catalog sweep
            # drops it from home/search instead of showing an
            # unplayable movie with a fake «Українська» track.
            raise ProviderError(ProviderErrorCode.GATED, "no player on content page")
        translations: list[Translation] = [
            Translation(
                id=_translation_id(t.label),
                label=t.label or "Українська",
            )
            for t in translations_models
        ]
        if not translations:
            translations = [Translation(id="uk", label="Українська")]
        seasons: list[Season] | None = None
        if translations_models:
            seasons = self._build_seasons(translations_models, external_id, self.id)
        mb_form, mb_styles = model_b_axes(media_type)  # type: ignore[arg-type]
        return ContentResponse(
            id=f"doramyworld:{external_id}",
            title=title,
            year=year,
            description=description,
            poster=poster,
            translations=translations,
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

    @staticmethod
    def _build_seasons(
        translations: list[_PlayerTranslation], external_id: str, provider_id: str
    ) -> list[Season]:
        """Flatten the data-player translations into one Season[].

        The upstream Kotlin uses two ``Episode`` lists (Dubbed/Subbed);
        the data-player format here is more expressive — each translation
        has its own seasons — but we surface only the first translation's
        seasons. Subsequent translations would need a separate
        translation-id selector on the API to address per-translation
        playback; that is a v3 concern."""
        if not translations:
            return []
        first = translations[0]
        seasons: list[Season] = []
        for s_idx, season in enumerate(first.seasons, start=1):
            episodes = [
                Episode(
                    number=e_idx,
                    id=f"{provider_id}:{external_id}:s{s_idx}e{e_idx}",
                    title=f"Серія {e_idx}",
                )
                for e_idx, _ in enumerate(season.episodes, start=1)
                if _.strip()
            ]
            if not episodes:
                continue
            seasons.append(Season(number=s_idx, episodes=episodes))
        return seasons or []

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` arrives as "<external_id>:s<N>e<M>". The API
        # strips the `<provider>:` prefix before calling us, so the
        # path through `/api/stream` is straightforward.
        ext_id, ep_suffix = split_content_suffix(content_id)
        if not _EXTERNAL_ID_RE.fullmatch(ext_id):
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"bad external_id: {ext_id!r}")
        if not _EP_SUFFIX_RE.fullmatch(ep_suffix):
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"bad episode suffix: {ep_suffix!r}")
        url = f"{BASE_URL}/{ext_id}/"
        try:
            resp = await self.guarded_get(http, url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        player_models = _parse_player(resp.text)
        if not player_models:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no data-player on content page")
        ashdi_url = self._select_player_url(player_models, ep_suffix)
        if ashdi_url is None:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"no player url for {ep_suffix!r}")
        # ashdi.vip serves a page with `file:'...m3u8...'`. The shared
        # RegexExtractor picks that pattern up cleanly. The URL came
        # from upstream HTML, so it goes through the redirect
        # allowlist (#126).
        try:
            ashdi_resp = await self.guarded_get(
                http, ashdi_url, headers={"Referer": ASHDI_REFERER}
            )
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if ashdi_resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {ashdi_resp.status_code}")
        extracted = RegexExtractor().extract(ashdi_resp.text)
        if extracted is None or not extracted.url:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no m3u8 in ashdi page")
        return StreamResponse(
            url=extracted.url,
            type=extracted.type,
            headers={"Referer": ASHDI_REFERER, "User-Agent": "cs-uk-api/1.0"},
        )

    @staticmethod
    def _select_player_url(
        translations: list[_PlayerTranslation], ep_suffix: str
    ) -> str | None:
        """Resolve the ashdi URL for a season/episode suffix.

        Returns None when the suffix is malformed or out of range so the
        caller can surface an explicit ``not_found``. There is no silent
        ``first available episode`` fallback -- that would mask a missing
        suffix in the caller.
        """
        m = _EP_SUFFIX_RE.fullmatch(ep_suffix)
        if not m:
            return None
        s_idx, e_idx = int(m.group(1)), int(m.group(2))
        if not translations:
            return None
        first = translations[0]
        if not (1 <= s_idx <= len(first.seasons)):
            return None
        season = first.seasons[s_idx - 1]
        if not (1 <= e_idx <= len(season.episodes)):
            return None
        url = season.episodes[e_idx - 1]
        return url if url else None


__all__ = ["DoramyWorldProvider"]
