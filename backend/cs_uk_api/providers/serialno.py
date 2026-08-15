"""Serialno provider (https://serialno.tv) — Ukrainian-dubbed series.
Issue #17, Group 2.

The site is a DLE-style CMS whose homepage IS the series listing
(there is no separate `/series/` path), so the v2 contract exposes a
single `series` section backed by `/` and `/page/N/`.

The stream chain is two-hop:

  content page → `tortuga.tw/embed/<id>` (first `.fplayer iframe`)
  → obfuscated ``file:`` payload decoded with the upstream
  ``Decoder.torDecrypt`` algorithm — same shape as KinoVezha.

The second `.fplayer iframe` is a trailer (it decodes to a
``hls/trailers/...`` URL) and is ignored.

External-id shape: the bare slug `<id>-<title>` (e.g. `2075-1670`),
no section prefix.
"""
from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..country import extract_country
from ..models import (
    ContentResponse,
    Episode,
    Person,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from ._tortuga import decode as _tor_decrypt
from .base import (
    BaseProvider,
    ProviderError,
    ProviderErrorCode,
    model_b_axes,
    split_content_suffix,
)

BASE_URL = "https://serialno.tv"


def _parse_fmeta(soup: BeautifulSoup) -> tuple[int | None, list[str], list[Person]]:
    """Year + people from the `.flist` info rows.

    The DLE template renders `<li><span>Рік:</span> <a>2023</a> …`,
    `<li><span>Режисер:</span> <a>…</a>, <a>…</a>`, and
    `<li><span>В ролях:</span> <a>…</a>, …`. The parser ignored the
    block entirely, so every serialno title showed no year and an
    empty People rail even though the data is on the page (ticket
    #227).
    """
    year: int | None = None
    genres: list[str] = []
    people: list[Person] = []
    for li in soup.select("ul.flist li"):
        label_el = li.select_one("span")
        if label_el is None:
            continue
        label = label_el.get_text(strip=True).rstrip(":")
        if label == "Рік":
            m = re.search(r"(20\d{2})", li.get_text(" ", strip=True))
            if m:
                year = int(m.group(1))
        elif label == "Жанр":
            genres = [g for g in (a.get_text(strip=True) for a in li.select("a")) if g]
        elif label in ("Режисер", "В ролях"):
            role = "Director" if label == "Режисер" else "Actor"
            for a in li.select("a"):
                name = a.get_text(strip=True)
                if not name:
                    continue
                # The xfsearch href ends with the person's name; the
                # display name must be the final segment so /Persons/
                # round-trips (kinotron convention).
                people.append(Person(id=f"serialno:{name}", name=name, role=role))
    return year, genres, people
#: A dubbing label inside the ``{...}`` prefix of a live flat-payload
#: file value (e.g. ``{КІНО}https://calypso.tortuga.tw/...``, ticket #332).
_DUB_PREFIX_RE = re.compile(r"^\{(.*?)\}")


def _dub_prefix(file_value: str) -> str | None:
    m = _DUB_PREFIX_RE.match(file_value)
    return m.group(1).strip() if m else None


def _season_list(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the decoded player payload to a season list.

    Two shapes appear in the wild:
      - flat (current live): ``data`` IS the season list — each
        top-level item is a season whose ``folder`` holds episodes;
      - dub-wrapped (older fixtures): ``data`` is a list of dubs
        whose first dub's ``folder`` holds seasons.

    Detect by whether the first folder's children look like episodes
    (no nested ``folder`` key); return the season list either way.
    """
    if not data:
        return []
    first = data[0]
    first_folder = first.get("folder") or []
    is_flat = not first_folder or all(
        isinstance(x, dict) and "folder" not in x for x in first_folder
    )
    season_list = data if is_flat else first_folder
    return season_list if isinstance(season_list, list) else []

# Sections exposed by Serialno. Per the upstream Kotlin ``mainPage``
# and the spec note, the site is series-only — the homepage lists
# series and there is no film/cartoon section in scope.
SERIALNO_SECTIONS: tuple[Section, ...] = (
    Section(id="series", title="Серіали", form="series"),
)

# Slug regex: the site uses Latin transliteration of Ukrainian titles
# (e.g. `2075-1670`, `29-seks-i-misto`). Enforced at the provider
# boundary so path-traversal / injection attempts surface as
# `not_found` BEFORE any HTTP request is made.
_SLUG_RE = re.compile(r"\d+-[a-z0-9-]+")

# Pagination link matcher — DLE convention `/page/N/`.
_PAGINATION_LINK = re.compile(r"/page/(\d+)/?")

# The Kotlin fileRegex captures the obfuscated `file:"…"` payload
# from an inline script on the tortuga.tw player page.
_FILE_RE = re.compile(r"""file\s*:\s*["']([^"']+)["']""")


def _page_number(href: str) -> int:
    m = _PAGINATION_LINK.search(href)
    return int(m.group(1)) if m else 0


def _external_id_from_url(href: str) -> str | None:
    """Return the URL slug (e.g. ``2075-1670``) from a card link.

    Serialno URLs are bare slugs of the shape ``<id>-<title>.html``
    with no section prefix."""
    m = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href, re.IGNORECASE)
    return m.group(1) if m else None


def _section_url(section: str, page: int) -> str:
    """Build the listing URL for a section + page.

    Serialno's homepage IS the series listing; DLE pagination
    moves subsequent pages to ``/page/N/``."""
    if section not in {"series"}:
        raise ProviderError(ProviderErrorCode.NOT_FOUND, f"unknown section: {section}")
    base = f"{BASE_URL}/"
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one ``.th-item`` listing card.

    Each card wraps an anchor ``<a class="th-in" href="...">`` whose
    child ``.th-title`` holds the title; the poster is the lazy
    ``<img data-src="/uploads/...">`` inside ``.th-img``."""
    a = card.select_one("a.th-in")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    title_el = card.select_one(".th-title")
    title = title_el.get_text(strip=True) if title_el else a.get_text(" ", strip=True)
    img = card.select_one(".th-img img")
    poster_src: str | None = None
    if img is not None:
        poster_src = str(img.get("data-src") or img.get("src") or "") or None
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    external_id = _external_id_from_url(href)
    if not external_id:
        return None
    mb_form, mb_styles = model_b_axes("series")
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


def _resolve_file_value(html: str) -> str | None:
    """Pull the obfuscated ``file:"…"`` value out of every inline
    ``<script>`` on the player page, then run the upstream decoder.
    Returns the first value whose decode starts with ``http`` (a
    direct m3u8) or ``[`` (a season/episode JSON list)."""
    soup = BeautifulSoup(html, "lxml")
    for script in soup.select("script"):
        m = _FILE_RE.search(script.get_text())
        if not m:
            continue
        decoded = _tor_decrypt(m.group(1))
        if decoded and decoded.startswith(("http", "[")):
            return decoded
    return None


def _parse_player_json(raw: str) -> list[dict[str, Any]]:
    """Parse the series player JSON. The upstream Kotlin uses a
    lenient parser; structural problems surface as a JSON error so
    the caller can raise ``parse_failed``."""
    return cast(list[dict[str, Any]], json.loads(raw))


class SerialnoProvider(BaseProvider):
    id = "serialno"
    name = "Serialno"
    types = ("series",)
    sections = SERIALNO_SECTIONS
    #: SSRF allowlist (spec #309 T8): the DLE CMS and the tortuga
    #: player. A hostile CMS response must not be able to pivot either
    #: hop to an attacker-controlled host. ``guarded_get`` applies this
    #: by default.
    hosts = frozenset({"serialno.tv", "tortuga.tw"})

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # DLE search form posts to /index.php?do=search with the
        # standard ``do`` / ``subaction`` / ``story`` fields. The
        # upstream Kotlin sends the same payload; we quote the query
        # (httpx form-encodes the rest).
        try:
            resp = await http.post(
                f"{BASE_URL}/index.php?do=search",
                data={"do": "search", "subaction": "search", "story": quote(query)},
            )
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(
                ProviderErrorCode.UPSTREAM_UNREACHABLE, f"status {resp.status_code}"
            )
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select(".th-item"):
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
        for card in soup.select(".th-item"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # Pagination lives inside `<div class="navigation">` with
        # sibling anchors to `/page/N/`. The current page is a
        # `<span>`; any sibling `<a>` to a higher page number means
        # there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("div.navigation a[href*='/page/']")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"bad external_id: {external_id!r}")
        url = f"{BASE_URL}/{external_id}.html"
        try:
            resp = await self.guarded_get(http, url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        # Title selector: the content page's `<h1>` is the show
        # title (e.g. "1670").
        title_el = soup.select_one("h1")
        if title_el is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "title missing")
        # Poster: the `<img>` inside `.fposter`. The page lazy-loads
        # it via `data-src`; some pages set `src` directly.
        img = soup.select_one(".fposter img")
        poster_src: str | None = None
        if img is not None:
            poster_src = str(img.get("data-src") or img.get("src") or "") or None
        poster = urljoin(BASE_URL, poster_src) if poster_src else None
        # Description: `.fdesc` is the page's prose blurb. The
        # block may start with an `<h2>` ("Коротко про серіал …");
        # we accept the whole block and surface its text.
        desc_el = soup.select_one(".fdesc")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""
        year_int, genres, people = _parse_fmeta(soup)
        country: str | None = extract_country(soup)
        # Player URL: the first `<iframe>` inside `.fplayer` is the
        # series player (`tortuga.tw/embed/<id>`); the second is a
        # trailer and is ignored.
        iframe = soup.select_one(".fplayer iframe")
        if iframe is None or not iframe.get("src"):
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no player iframe on content page")
        player_url = str(iframe["src"])
        seasons, translations = await self._load_series_seasons(player_url, external_id, http, self.id)
        mb_form, mb_styles = model_b_axes("series")
        return ContentResponse(
            id=f"serialno:{external_id}",
            title=title_el.get_text(strip=True),
            description=description,
            year=year_int,
            poster=poster,
            genres=genres,
            people=people,
            translations=translations or [Translation(id="uk", label="Українська")],
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

    async def _load_series_seasons(
        self, player_url: str, external_id: str, http: httpx.AsyncClient, provider_id: str
    ) -> tuple[list[Season] | None, list[Translation]]:
        """Fetch the tortuga.tw player page and decode the obfuscated
        season/episode JSON list.

        The decoded ``file:`` payload for a series starts with ``[``
        and is either a list of "dub" objects, each with a ``folder:
        [...]`` of seasons (dub-wrapped), or a flat list of seasons
        whose episodes carry a ``{DUB_LABEL}`` prefix on their file
        value. Both shapes carry dubbing-studio names that the dub
        picker (spec #276) must see as translations (ticket #332).

        Returns ``(None, [])`` on parse failure so the caller surfaces
        an empty seasons list — the live gate will then see no episodes
        and stop, instead of crashing."""
        try:
            resp = await self.guarded_get(http, player_url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        decoded = _resolve_file_value(resp.text)
        if decoded is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no file value on player page")
        if not decoded.startswith("["):
            raise ProviderError(
                ProviderErrorCode.PARSE_FAILED, "player payload is not a season/episode list"
            )
        try:
            data = _parse_player_json(decoded)
        except json.JSONDecodeError as e:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, f"player json: {e}") from e
        seasons: list[Season] = []
        if not data:
            return None, []
        season_list = _season_list(data)
        if not season_list:
            return None, []
        # Dub-wrapped shape: the top-level entries are dubbing tracks
        # (studio titles) whose ``folder`` holds the seasons (ticket #332).
        # Mirrors ``_season_list``'s shape detection: children carrying a
        # nested ``folder`` key = dubs; flat seasons carry episodes.
        first_entry = data[0] if data else {}
        first_folder = first_entry.get("folder") or [] if isinstance(first_entry, dict) else []
        is_dub_wrapped = bool(first_folder) and any(
            isinstance(x, dict) and "folder" in x for x in first_folder
        )
        wrapped_dubs = (
            [
                str(d.get("title", "")).strip()
                for d in data
                if isinstance(d, dict) and str(d.get("title", "")).strip()
            ]
            if is_dub_wrapped
            else []
        )
        # Flat shape: dub labels live in the ``{...}`` prefixes of the
        # episode file values.
        flat_dubs: list[str] = []
        for season in season_list:
            if not isinstance(season, dict):
                continue
            for ep in season.get("folder") or []:
                if isinstance(ep, dict):
                    label = _dub_prefix(str(ep.get("file", "")))
                    if label and label not in flat_dubs:
                        flat_dubs.append(label)
        dub_titles = wrapped_dubs or flat_dubs
        for s_idx, season in enumerate(season_list, start=1):
            if not isinstance(season, dict):
                continue
            episodes_raw = season.get("folder") or []
            if not isinstance(episodes_raw, list):
                continue
            episodes: list[Episode] = []
            for e_idx, ep in enumerate(episodes_raw, start=1):
                if not isinstance(ep, dict):
                    continue
                ep_title = str(ep.get("title", "")).strip() or f"Серія {e_idx}"
                ep_label = _dub_prefix(str(ep.get("file", "")))
                ep_dubs = [ep_label] if ep_label else dub_titles
                episodes.append(Episode(
                    number=e_idx,
                    id=f"{provider_id}:{external_id}:s{s_idx}e{e_idx}",
                    title=ep_title,
                    translations=[Translation(id=t, label=t) for t in ep_dubs if t] or None,
                ))
            if episodes:
                seasons.append(Season(number=s_idx, episodes=episodes))
        translations = [Translation(id=t, label=t) for t in dub_titles if t]
        return (seasons or None), translations

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` arrives as either "<external>" (no suffix)
        # or "<external>:s<N>e<M>" (series episode) — the shared suffix
        # splitter handles both (spec #309 T8). `/api/stream` strips the
        # `<provider>:` prefix before calling us. The external_id alone
        # is enough to rebuild the content URL.
        ext_id, ep_suffix = split_content_suffix(content_id)
        if not _SLUG_RE.fullmatch(ext_id):
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"bad external_id: {ext_id!r}")
        content_url = f"{BASE_URL}/{ext_id}.html"
        try:
            resp = await self.guarded_get(http, content_url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        iframe = soup.select_one(".fplayer iframe")
        if iframe is None or not iframe.get("src"):
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no player iframe on content page")
        player_url = str(iframe["src"])
        try:
            player_resp = await self.guarded_get(http, player_url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {player_resp.status_code}")
        decoded = _resolve_file_value(player_resp.text)
        if decoded is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no file value on player page")
        if not decoded.startswith("["):
            raise ProviderError(
                ProviderErrorCode.PARSE_FAILED, "player payload is not a season/episode list"
            )
        stream_url = self._select_stream_url(decoded, ep_suffix, translation)
        if stream_url is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, f"no stream url for {ep_suffix!r}")
        return StreamResponse(
            url=stream_url,
            type="m3u8",
            headers={"Referer": BASE_URL + "/", "User-Agent": "cs-uk-api/1.0"},
        )

    @staticmethod
    def _select_stream_url(decoded: str, ep_suffix: str, translation: str | None = None) -> str | None:
        """Resolve the m3u8 URL for a series episode (the season/
        episode JSON list).

        The dub picker's translation id (spec #276) selects the
        matching dubbing track (ticket #332): on the dub-wrapped shape
        the top-level entries are dubs titled by studio, on the flat
        shape the episode files carry a ``{DUB_LABEL}`` prefix. An
        unmatched translation falls back to the default track.

        Returns ``None`` for missing / malformed / out-of-range
        suffixes so the caller surfaces ``parse_failed`` rather than
        silently returning the first available episode (a known
        regression pattern caught by code-reviewer on KinoTron)."""
        if not ep_suffix:
            return None
        m = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
        if not m:
            return None
        s_idx = int(m.group(1))
        e_idx = int(m.group(2))
        try:
            data = _parse_player_json(decoded)
        except json.JSONDecodeError:
            return None
        if not data:
            return None
        if translation:
            # Dub-wrapped: pick the dub whose title matches, then the
            # same season/episode indexes inside it.
            for dub in data:
                if not isinstance(dub, dict):
                    continue
                if str(dub.get("title", "")).strip() != translation:
                    continue
                seasons = dub.get("folder") or []
                if not isinstance(seasons, list) or s_idx > len(seasons):
                    continue
                season = seasons[s_idx - 1]
                episodes_raw = season.get("folder") if isinstance(season, dict) else []
                if isinstance(episodes_raw, list) and 1 <= e_idx <= len(episodes_raw):
                    ep = episodes_raw[e_idx - 1]
                    if isinstance(ep, dict):
                        return SerialnoProvider._clean_file_value(str(ep.get("file", ""))) or None
            # Fall through to the default track when no dub matched.
        season_list = _season_list(data)
        if not season_list:
            return None
        if s_idx < 1 or s_idx > len(season_list):
            return None
        season = season_list[s_idx - 1]
        if not isinstance(season, dict):
            return None
        episodes_raw = season.get("folder") or []
        if not isinstance(episodes_raw, list) or e_idx < 1 or e_idx > len(episodes_raw):
            return None
        if translation:
            # Flat shape: prefer the episode whose ``{DUB_LABEL}``
            # prefix matches the picked translation.
            candidates = [
                ep for ep in episodes_raw
                if isinstance(ep, dict) and _dub_prefix(str(ep.get("file", ""))) == translation
            ]
            if candidates:
                ep = candidates[e_idx - 1] if e_idx <= len(candidates) else candidates[0]
            else:
                ep = episodes_raw[e_idx - 1]
        else:
            ep = episodes_raw[e_idx - 1]
        if not isinstance(ep, dict):
            return None
        return SerialnoProvider._clean_file_value(str(ep.get("file", ""))) or None

    @staticmethod
    def _clean_file_value(file_value: str) -> str:
        """Strip the ``{DUB_LABEL}`` prefix and the ``(subtitle:...)``
        marker from a live Tortuga file value so the client receives a
        bare m3u8 URL."""
        if file_value.startswith("{"):
            file_value = file_value.split("}", 1)[1]
        if "(subtitle:" in file_value:
            file_value = file_value.split("(subtitle:", 1)[0]
        return file_value


__all__ = ["SerialnoProvider"]
