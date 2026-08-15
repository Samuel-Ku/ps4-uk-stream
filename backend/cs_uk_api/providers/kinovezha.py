"""KinoVezha provider (https://kinovezha.tv) — Ukrainian-dubbed films,
serials, cartoons and mult-serials. Issue #17, Group 1.

The upstream player URL lives on `tortuga.tw` and embeds an obfuscated
`file:` value in an inline `<script>`. The Kotlin source's
``Decoder.torDecrypt`` resolves it by XOR-decoding a base64 payload
whose first byte is the salt. A successful decode starts with
``http`` (a direct m3u8) or ``[`` (a season/episode JSON list).

Note: search results carry no kind prefix in their URLs, so the
``SearchResult.type`` returned by ``search()`` is best-effort and
defaults to ``movie``. The browse helper overrides this per-section.
The real type is resolved authoritatively by ``/api/content`` from
the Жанр list on the content page."""
from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from ..country import extract_country
from ..http_client import safe_get
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from ..wire_identity import MOVIE_SUFFIX
from ._tortuga import decode as _tor_decrypt
from .base import BaseProvider, ProviderError, model_b_axes

BASE_URL = "https://kinovezha.tv"
# Hosts the upstream may legally redirect to: the DLE CMS and the
# tortuga player. A hostile CMS response must not be able to pivot
# either hop to an attacker-controlled host.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"kinovezha.tv", "tortuga.tw"})

# Sections exposed by KinoVezha's main navigation. Per the upstream
# Kotlin source's `mainPage = mainPageOf(...)`:
#   films -> Фільми, series -> Серіали,
#   cartoons -> Мультфільми, s-cartoons -> Мультсеріали.
KINOVEZHA_SECTIONS: tuple[Section, ...] = (
    Section(id="films", title="Фільми", form="movie"),
    Section(id="series", title="Серіали", form="series"),
    Section(id="cartoons", title="Мультфільми", form="movie"),
    Section(id="s-cartoons", title="Мультсеріали", form="series"),
)

# Section path -> external kind prefix for `_classify_from_url`.
# KinoVezha URLs use bare slugs (e.g. `/2831-enn-droyid.html`), so the
# kind is encoded in the section path the card came from, not in the
# URL itself. The mapping below is used by the browse helper.
_SECTION_KIND: dict[str, str] = {
    "films": "movie",
    "series": "series",
    "cartoons": "movie",
    "s-cartoons": "series",
}

# Path segment -> MediaType. Order matters: longest prefixes first so
# `/s-cartoons/` is classified as `series` (not `movie` via `cartoon`).
# Per the upstream Kotlin's conditional: tags contain "Мультсеріали"
# or "Серіали" → TvSeries, else TvType.Movie.
_PATH_TYPE: tuple[tuple[str, str], ...] = (
    ("s-cartoons", "series"),  # /s-cartoons/ — Мультсеріали
    ("series", "series"),      # /series/ — Серіали
    ("cartoon", "movie"),      # /cartoons/ — Мультфільми
    ("film", "movie"),         # /films/ — Фільми
)

# Жанр tag -> MediaType. Used by `content()` to classify the page from
# the tag list (the Kotlin conditional: contains "Мультсеріали" or
# "Серіали" → TvSeries; else Movie).
# Needles are pre-lowered to skip `.lower()` on every call. Both the
# singular and plural forms are needed — upstream titles a Мультсеріал
# page's Жанр row «Мультсеріал» (singular, observed live 2026-08-09).
# Longest needles first so "Мультсеріали" beats "Серіали".
_TAG_TYPE: tuple[tuple[str, str], ...] = (
    ("мультсеріали", "series"),
    ("мультсеріал", "series"),
    ("серіали", "series"),
    ("серіал", "series"),
    ("мультфільми", "movie"),
    ("фільми", "movie"),
)

# KinoVezha's listing pagination lives in `<div class="pagination"
# id="pagination">` with sibling anchors. The page number is the trailing
# integer in `/page/N/`.
_PAGINATION_LINK = re.compile(r"/page/(\d+)/?")

# The Kotlin fileRegex captures the obfuscated `file:"…"` payload from
# an inline script on the player page.
_FILE_RE = re.compile(r"""file\s*:\s*["']([^"']+)["']""")

# Episode-id suffix for movies (whose Player iframe is a single URL
# rather than a season/episode map; defined once in ``wire_identity``,
# spec #309).

# external_id is a numeric-prefixed slug (e.g. "2831-enn-droyid"). Gate
# both content() and stream() against values that could escape the URL
# path before interpolation.
_SLUG_RE = re.compile(r"\d+-[a-z0-9][a-z0-9-]*")


def _classify_from_tags(tags_text: str) -> str:
    """Map the Жанр list text to a MediaType. Mirrors the upstream
    conditional: contains "Мультсеріали" or "Серіали" → series; else
    movie. Longest-prefix-first so "Мультсеріали" beats "Серіали"."""
    lower = tags_text.lower()
    for needle, t in _TAG_TYPE:
        if needle in lower:
            return t
    return "movie"


def _classify_from_url(href: str) -> str:
    """Map a section path to a MediaType. Used by the browse helper."""
    lower = href.lower()
    for needle, t in _PATH_TYPE:
        if f"/{needle}" in lower:
            return t
    return "movie"


def _page_number(href: str) -> int:
    m = _PAGINATION_LINK.search(href)
    return int(m.group(1)) if m else 0


def _section_url(section: str, page: int) -> str:
    paths = {
        "films": "/films",
        "series": "/series",
        "cartoons": "/cartoons",
        "s-cartoons": "/s-cartoons",
    }
    if section not in paths:
        raise ProviderError("not_found", f"unknown section: {section}")
    # The upstream Kotlin always requests `$url/page/` (no trailing
    # slash) + `page` integer. DLE serves both shapes, but the page
    # block only renders at the `/page/N/` shape, so we mirror that.
    return f"{BASE_URL}{paths[section]}/page/{page}/"


def _external_id_from_url(href: str) -> str | None:
    """Return the URL slug (e.g. "2831-enn-droyid") from a card link."""
    m = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href, re.IGNORECASE)
    return m.group(1) if m else None


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one `.movie-item` listing card.

    The card's outer `<a class="movie-item__link" href="...">` is the
    title link; the poster is the `<img data-src="/posters/...">`
    inside `.movie-item__img`. The year is the first anchor in
    `.movie-item__meta` (a `<a href="/year/YYYY/">YYYY</a>`).

    KinoVezha URLs are bare slugs (e.g. `/2831-enn-droyid.html`) with
    no kind prefix, so ``_classify_from_url`` defaults to ``movie``.
    The browse helper applies a per-section kind override; ``search``
    does not — see the module docstring. The real type comes from
    ``/api/content``."""
    a = card.select_one("a.movie-item__link")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    title_el = card.select_one(".movie-item__title")
    title = title_el.get_text(strip=True) if title_el else a.get_text(" ", strip=True)
    img = card.select_one(".movie-item__img img")
    poster_src = str(img.get("data-src") or img.get("src")) if img and (img.get("data-src") or img.get("src")) else None
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    external_id = _external_id_from_url(href)
    if not external_id:
        return None
    mb_form, mb_styles = model_b_axes(_classify_from_url(href))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


def _parse_cards(html: str, provider_id: str) -> list[SearchResult]:
    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []
    for card in soup.select(".movie-item"):
        parsed = _parse_card(card, provider_id)
        if parsed is not None:
            results.append(parsed)
    return results


def _resolve_file_value(html: str) -> str | None:
    """Pull the obfuscated `file:"…"` value out of the inline scripts on
    the player page, then run the upstream decoder. The Kotlin code
    looks at every `<script>` on the page and applies its `fileRegex`
    to each; we do the same."""
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
    """Parse the series player JSON. The upstream Kotlin uses
    ``AppUtils.tryParseJson<List<PlayerJson>>`` which is lenient — the
    captured fixture contains ``"number#:"2"`` (corrupted from
    ``"number":"2"``) inside an otherwise-valid array. We fix that one
    known corruption before feeding json.loads; structural problems
    surface as a parse error so the caller can raise ``parse_failed``.
    """
    fixed = re.sub(r'"([a-zA-Z_][a-zA-Z0-9_]*)#:"', r'"\1":"', raw)
    return cast(list[dict[str, Any]], json.loads(fixed))


class KinoVezhaProvider(BaseProvider):
    id = "kinovezha"
    name = "КіноВежа"
    types = ("movie", "series")
    sections = KINOVEZHA_SECTIONS

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # DLE-style POST search. The site is a DLE CMS so the same
        # fields as CikavaIdeya / UFDub work.
        try:
            resp = await http.post(
                BASE_URL,
                data={"do": "search", "subaction": "search", "story": quote(query)},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
        return _parse_cards(resp.text, self.id)

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        url = _section_url(section, page)
        # The upstream now 301-redirects the first page (`/films/page/1/`
        # -> `/films/`), so fetch through the SSRF-safe `safe_get` helper
        # (same host allowlist as stream()) which follows allowed same-host
        # redirects. Pages > 1 still return 200 directly and are unaffected.
        try:
            resp = await safe_get(
                http,
                url,
                allowed_hosts=set(_ALLOWED_HOSTS),
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        results = _parse_cards(resp.text, self.id)
        # Pagination: `<div class="pagination" id="pagination">` with
        # sibling anchors to `/section/page/N/`. Any link to a higher
        # page than `page` means there is a next page.
        soup = BeautifulSoup(resp.text, "lxml")
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("div#pagination a[href*='/page/']")
        )
        # Section kind overrides per-card URL classification — the path
        # itself doesn't always carry a kind prefix on browse listings.
        # Contract #135: the override lands on ``form`` (the section is
        # form-only; the legacy ``type`` field is gone).
        kind = _SECTION_KIND.get(section)
        if kind:
            results = [
                r.model_copy(update={"form": kind})
                for r in results
            ]
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        url = f"{BASE_URL}/{external_id}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one(".inner-page__title")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        # Poster is the first `<img>` inside the `.inner-page__poster`
        # block — same `.img-fit-cover img` selector as the upstream
        # `load()` function. Some content pages use `data-src` (lazy
        # load); prefer it, fall back to `src`.
        img = soup.select_one(".img-fit-cover img")
        poster_src: str | None = None
        if img is not None:
            poster_src = str(img.get("src") or img.get("data-src") or "") or None
        poster = urljoin(BASE_URL, poster_src) if poster_src else None
        # The Жанр row is the `<li>` whose label is «Жанр» inside
        # `.inner-page__list`. It used to sit at a fixed index (2), but
        # upstream dropped the «Списки:» row so the position shifted
        # (observed live 2026-08-09) — index-based lookup misread the
        # row as «Країна: США» and classified a Мультсеріал as a movie,
        # dead at stream() time. Match the label instead.
        flist = soup.select(".inner-page__list > li")
        tags_text = ""
        for li in flist:
            label = li.select_one("span")
            if label is not None and "Жанр" in label.get_text(strip=True):
                tags_text = li.get_text(" ", strip=True)
                break
        media_type = _classify_from_tags(tags_text)
        country: str | None = extract_country(soup)
        desc_el = soup.select_one("div.inner-page__text")
        description = desc_el.get_text(strip=True) if desc_el else ""
        # Player URL: the first iframe inside `.video-responsive` (or
        # any iframe when no wrapper exists). The series player is the
        # one that embeds the season/episode JSON list; for movies it
        # is the first `<iframe>` with a non-empty src.
        player_url = self._extract_player_url(soup)
        seasons: list[Season] | None = None
        if media_type == "series" and player_url:
            seasons = await self._load_series_seasons(player_url, external_id, http, self.id)
        elif player_url:
            seasons = [Season(number=1, episodes=[Episode(
                number=1, id=f"{self.id}:{external_id}{MOVIE_SUFFIX}", title=title_el.get_text(strip=True),
            )])]
        mb_form, mb_styles = model_b_axes(media_type)  # type: ignore[arg-type]
        return ContentResponse(
            id=f"kinovezha:{external_id}",
            title=title_el.get_text(strip=True),
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

    @staticmethod
    def _extract_player_url(soup: BeautifulSoup) -> str | None:
        # Mirrors the upstream: `document.select(".video-responsive >
        # iframe").attr("src")`. We accept either the wrapped or bare
        # selector so the bare `.inner-page__player iframe` (used when
        # the page lacks the `.video-responsive` wrapper) still parses.
        iframe = soup.select_one(".video-responsive > iframe")
        if iframe is None or not iframe.get("src"):
            iframe = soup.select_one(".inner-page__player iframe")
        if iframe is None or not iframe.get("src"):
            return None
        return str(iframe["src"])

    @staticmethod
    async def _load_series_seasons(
        player_url: str, external_id: str, http: httpx.AsyncClient, provider_id: str
    ) -> list[Season] | None:
        try:
            resp = await safe_get(
                http,
                player_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        decoded = _resolve_file_value(resp.text)
        if decoded is None:
            raise ProviderError("parse_failed", "no file value on player page")
        if not decoded.startswith("["):
            raise ProviderError("parse_failed", "player payload is not a season/episode list")
        try:
            data = _parse_player_json(decoded)
        except json.JSONDecodeError as e:
            raise ProviderError("parse_failed", f"player json: {e}") from e
        seasons: list[Season] = []
        for s_idx, season in enumerate(data, start=1):
            episodes_raw = season.get("folder") or []
            if not isinstance(episodes_raw, list):
                continue
            episodes: list[Episode] = []
            for e_idx, ep in enumerate(episodes_raw, start=1):
                if not isinstance(ep, dict):
                    continue
                ep_title = str(ep.get("title", "")).strip() or f"Серія {e_idx}"
                episodes.append(Episode(
                    number=e_idx,
                    id=f"{provider_id}:{external_id}:s{s_idx}e{e_idx}",
                    title=ep_title,
                ))
            if episodes:
                seasons.append(Season(number=s_idx, episodes=episodes))
        return seasons

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` arrives as either "<external_id>" (movie, single
        # URL), "<external_id>:__movie__" (movie, explicit suffix from
        # the content listing), or "<external_id>:s<N>e<M>" (series
        # episode). `/api/stream` strips the `<provider>:` prefix before
        # calling us. The external_id alone must be enough to rebuild
        # the content URL — calling `http.get(content_id)` raises
        # `ValueError: unknown url type` (caught by code-reviewer on
        # UFDub).
        if MOVIE_SUFFIX in content_id:
            ext_id = content_id.split(MOVIE_SUFFIX, 1)[0]
            ep_suffix = ""
        elif ":" in content_id:
            ext_id, _, ep_suffix = content_id.rpartition(":")
        else:
            ext_id = content_id
            ep_suffix = ""
        if not _SLUG_RE.fullmatch(ext_id):
            raise ProviderError("not_found", "bad external_id")
        content_url = f"{BASE_URL}/{ext_id}.html"
        try:
            resp = await http.get(content_url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        player_url = self._extract_player_url(soup)
        if player_url is None:
            raise ProviderError("parse_failed", "no player iframe on content page")
        # The player URL came from upstream HTML, so it goes through
        # the redirect allowlist (#126).
        try:
            player_resp = await safe_get(
                http,
                player_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError("not_found", f"status {player_resp.status_code}")
        decoded = _resolve_file_value(player_resp.text)
        if decoded is None:
            raise ProviderError("parse_failed", "no file value on player page")
        stream_url = self._select_stream_url(decoded, ep_suffix)
        if stream_url is None:
            raise ProviderError("parse_failed", f"no stream url for {ep_suffix!r}")
        return StreamResponse(url=stream_url, type="m3u8", headers={
            "Referer": BASE_URL + "/",
            "User-Agent": "cs-uk-api/1.0",
        })

    @staticmethod
    def _select_stream_url(decoded: str, ep_suffix: str) -> str | None:
        """Resolve the m3u8 URL for either a movie (single URL) or a
        series episode (JSON list of seasons + episodes).

        Returns ``None`` for out-of-range suffixes so the caller surfaces
        ``parse_failed`` rather than silently returning the first
        available episode (code-reviewer caught that fallback in
        KinoTron)."""
        if decoded.startswith("http"):
            return decoded if not ep_suffix else None
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
        if s_idx < 1 or s_idx > len(data):
            return None
        season = data[s_idx - 1]
        episodes_raw = season.get("folder") or []
        if not isinstance(episodes_raw, list) or e_idx < 1 or e_idx > len(episodes_raw):
            return None
        ep = episodes_raw[e_idx - 1]
        if not isinstance(ep, dict):
            return None
        file_value = str(ep.get("file", ""))
        # The series file value may carry an optional `{DUB_LABEL}`
        # prefix (e.g. `{OZZ}https://...`) and a trailing
        # `(subtitle:URL)` marker (empty when no subtitle is bundled).
        # Strip both so we hand the client a bare m3u8 URL.
        if file_value.startswith("{"):
            file_value = file_value.split("}", 1)[1]
        if "(subtitle:" in file_value:
            file_value = file_value.split("(subtitle:", 1)[0]
        return file_value or None


__all__ = ["KinoVezhaProvider"]