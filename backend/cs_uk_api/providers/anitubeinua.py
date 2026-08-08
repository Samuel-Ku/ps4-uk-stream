"""Anitubeinua provider (https://anitube.in.ua) — Ukrainian-dubbed and
subbed anime catalogue. Issue #17, Group 3.

The site is a DLE anime CMS whose listing lives at `/anime/page/N/` and
whose cards are `<article class="story">`. The content page exposes a
`<div class="playlists-ajax" data-xfname="playlist" data-news_id="...">`
container that the JS layer fills in via AJAX
(`/engine/ajax/playlists.php?news_id=...&xfield=playlist&user_hash=...`).
The AJAX response is a JSON envelope whose `response` is an HTML
fragment containing four `<div class="playlists-items">` blocks:

  0. Categories  (data-id: "0_0" СУБТИТРИ / "0_1" ОЗВУЧЕННЯ)
  1. Studios     (data-id: "0_0_0" / "0_1_0" / ...)
  2. Players     (data-id: "0_0_0_0" / "0_0_0_1" / ...)
  3. Episodes    (data-id: "0_0_0_0" / file URL / "1 серія")

The data-id is hierarchical: the first two segments identify the category
(SUBTITLES / DUB), the first three segments identify the studio, and the
full 4-segment id identifies the player. Episodes are grouped by the
first 3 segments of their data-id (the studio). Player URLs are stacked
in the same data-id, so the same episode is reachable via multiple
players (ashdi.vip / moonanime.art / peertube.in.ua).

For our public model:
- Each season corresponds to one category (season 1 = СУБТИТРИ,
  season 2 = ОЗВУЧЕННЯ).
- Each episode in a season has the same per-season number mapped to
  the studio's episode position (1 серія, 2 серія, ...).
- Per-episode translations are the studio names that have a file URL
  for that episode (e.g. "FanVoxUA").

External ID shape: `<news_id>-<slug>` (e.g.
`5981-vyskova-storya-malenkoyi-dvchinki-2-sezon`), validated by
`_EXTERNAL_ID_RE` at both `content()` and `stream()` boundaries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, ProviderError, model_b_axes

BASE_URL = "https://anitube.in.ua"
# ashdi.vip requires the upstream Referer to serve the m3u8 manifest.
# The upstream Kotlin sets `referer = "https://qeruya.cyou"` for the
# ashdi branch of `M3u8Helper.generateM3u8`; we mirror that.
ASHDI_REFERER = "https://qeruya.cyou"

# The one and only section: the site root path is `/anime/page/N/`.
ANITUBEINUA_SECTIONS: tuple[Section, ...] = (
    Section(id="page", title="Нові", type="anime"),
)

# External id: numeric news id, hyphen, slug. Used as a security
# boundary to refuse path traversal at content() and stream().
_EXTERNAL_ID_RE = re.compile(r"\d+-[a-z0-9-]+")

# Episode id pattern: `s<N>e<M>`. See module docstring for the season
# numbering (1 = SUB, 2 = DUB).
_EPISODE_RE = re.compile(r"s(\d+)e(\d+)")

# DLE login hash is read from the first <script> on the content page
# (matches the upstream `substringAfterLast("dle_login_hash = '")`).
_DLE_HASH_RE = re.compile(r"dle_login_hash\s*=\s*'([a-f0-9]+)'")

# `/xfsearch/year/NNNN/` link → year.
_YEAR_RE = re.compile(r"/xfsearch/year/(\d{4})/")

# File URL = m3u8. The player iframe host is detected from the URL
# prefix so we can set the right Referer.
_FILE_RE = re.compile(r"file\s*:\s*'([^']+\.m3u8)'")


@dataclass(frozen=True)
class _EpisodeRow:
    """One episode row from the playlist's third `.playlists-items` block."""

    data_id: str
    file_url: str
    title: str


def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _external_id_from_url(href: str) -> str | None:
    """Extract `<news_id>-<slug>` from the URL path. Returns None for
    unrecognised URLs so the caller can skip the card."""
    m = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href)
    return m.group(1) if m else None


def _extract_dle_hash(html: str) -> str:
    """Pull the dle_login_hash out of the content page HTML.

    The upstream Kotlin uses `body().selectFirst("script")` then
    `substringAfterLast("dle_login_hash = '")` on the script's HTML.
    The first <script> is often empty (the `<script src="...">` tags
    precede the inline ones), so we scan the rendered HTML for the
    last occurrence of the assignment — that mirrors the upstream
    `substringAfterLast` semantics and works regardless of script
    ordering."""
    last = html.rfind("dle_login_hash = '")
    if last < 0:
        raise ProviderError("parse_failed", "dle_login_hash not found")
    m = _DLE_HASH_RE.search(html[last:])
    if not m:
        raise ProviderError("parse_failed", "dle_login_hash not found")
    return m.group(1)


def _referer_for(player_url: str) -> str:
    """Pick the right Referer for the player URL. ashdi.vip matches the
    upstream `(referer = "https://qeruya.cyou")`; other hosts fall back
    to the site root, which is what the upstream uses for non-ashdi
    players."""
    if "ashdi.vip" in player_url:
        return ASHDI_REFERER
    return BASE_URL + "/"


def _parse_story(card: Any, provider_id: str) -> SearchResult | None:
    """Parse one `<article class="story">` listing card."""
    # Title selector: <h2> > <a> inside `.story_c`. The upstream Kotlin
    # uses `.story_c h2 a` (and falls back to `div.text_content a` for
    # the slider cards); we use the listing ancestor.
    title_a = card.select_one(".story_c h2 a")
    if title_a is None:
        title_a = card.select_one("div.text_content a")
    if title_a is None or not title_a.get("href"):
        return None
    href = str(title_a["href"])
    title = title_a.get_text(strip=True)
    if not title:
        return None
    external_id = _external_id_from_url(href)
    if external_id is None:
        return None
    # Poster: prefer `.story_c_l span.story_post img` (listing card);
    # fall back to the lazy-loaded `data-src` on the first <img> inside
    # the card (matches the upstream Kotlin fallback).
    img = card.select_one(".story_c_l span.story_post img")
    if img is None:
        img = card.select_one("a img")
    poster_src: str | None = None
    if img is not None:
        data_src = img.get("data-src")
        src = img.get("src")
        if isinstance(data_src, str) and data_src:
            poster_src = data_src
        elif isinstance(src, str) and src and not src.endswith("spacer.gif"):
            poster_src = src
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    mb_form, mb_styles = model_b_axes("anime")
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        type="anime",
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


def _attr_value(li: Any, name: str) -> str:
    """Read a single `<li>` attribute as a stripped string. `bs4`
    returns a list for multi-value attributes, so we coerce to
    `str` and strip."""
    raw = li.get(name) or ""
    if isinstance(raw, list):
        raw = " ".join(str(v) for v in raw)
    return str(raw).strip()


def _parse_content_meta(soup: BeautifulSoup) -> tuple[str, str | None, str, int | None]:
    """Pull title, poster, description, and year out of the content
    page soup. Raises `parse_failed` if the title is missing — the
    other fields are best-effort."""
    title_el = soup.select_one(".story_c h2")
    if title_el is None:
        raise ProviderError("parse_failed", "title missing")
    title = title_el.get_text(" ", strip=True)
    # Poster: `.story_c_left span.story_post img` (listing) or
    # `.story_post img` (older templates).
    poster_img = soup.select_one(".story_c_left span.story_post img")
    if poster_img is None:
        poster_img = soup.select_one("span.story_post img")
    poster_src: str | None = None
    if poster_img is not None:
        data_src = poster_img.get("data-src")
        src = poster_img.get("src")
        if isinstance(data_src, str) and data_src:
            poster_src = data_src
        elif isinstance(src, str) and src:
            poster_src = src
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    # Description: `.my-text` block (matches the upstream Kotlin).
    desc_el = soup.select_one("div.my-text")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    # Year: the first `<a href="/xfsearch/year/NNNN/">` link inside
    # the `.story_c_r` block.
    year: int | None = None
    year_link = soup.select_one(".story_c_r a[href*='/xfsearch/year/']")
    if year_link is not None:
        year_match = _YEAR_RE.search(str(year_link.get("href", "")))
        if year_match:
            year = int(year_match.group(1))
    return title, poster, description, year


def _parse_playlist(html: str) -> dict[str, Any]:
    """Parse the AJAX playlist response into a structured playlist.

    The HTML fragment normally contains four `<div class="playlists-items">`
    blocks: categories, studios, players, and episodes. Instead of
    relying on their position, we classify every `<li>` by its
    `data-id` depth and whether it carries a `data-file`:

      - 2 segments  -> category label (0_0 SUBTITLES / 0_1 DUB)
      - 3 segments  -> studio label (0_0_0 / 0_1_0 / ...)
      - 4 segments, no data-file -> player label (0_0_0_0 / ...)
      - data-file   -> episode row (data-id + player URL + title)

    This survives layout reflows — the live site briefly served only
    two blocks in 2026-08 — without silently losing episodes.

    Returns a dict with keys `categories`, `studios`, `players`, and
    `episodes` (list of `_EpisodeRow`)."""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.select(".playlists-items")
    categories: dict[str, str] = {}
    studios: dict[str, str] = {}
    players: dict[str, str] = {}
    episodes: list[_EpisodeRow] = []
    for block in blocks:
        for li in block.select("li"):
            data_id = _attr_value(li, "data-id")
            file_url = _attr_value(li, "data-file")
            segments = data_id.split("_") if data_id else []
            if file_url:
                episodes.append(
                    _EpisodeRow(
                        data_id=data_id,
                        file_url=file_url,
                        title=li.get_text(strip=True),
                    )
                )
            elif len(segments) == 2:
                categories[data_id] = li.get_text(strip=True)
            elif len(segments) == 3:
                studios[data_id] = li.get_text(strip=True)
            elif len(segments) >= 4:
                players[data_id] = li.get_text(strip=True)
    return {
        "categories": categories,
        "studios": studios,
        "players": players,
        "episodes": episodes,
    }


def _studio_id(ep_data_id: str) -> str:
    """First 3 segments of the data-id — identifies the studio."""
    segments = ep_data_id.split("_")
    return "_".join(segments[:3]) if len(segments) >= 3 else ""


def _category_id(ep_data_id: str) -> str:
    """First 2 segments of the data-id — identifies the category."""
    segments = ep_data_id.split("_")
    return "_".join(segments[:2]) if len(segments) >= 2 else ""


def _build_seasons(playlist: dict[str, Any], external_id: str) -> list[Season]:
    """Group the playlist into seasons.

    Each season corresponds to one category (SUB / DUB). Within a
    season, episodes are grouped by studio and labelled by their
    per-season number (1 серія, 2 серія, ...). The first studio
    reached is the canonical one for the season — the others are
    surfaced as per-episode translations.

    Returns a list of `Season` objects. Each season's `Episode` carries
    a list of `Translation` equal to the studio names that have a file
    URL for that episode."""
    categories = playlist["categories"]
    studios = playlist["studios"]
    episodes = playlist["episodes"]
    # Iterate in the canonical order: SUB (0_0) first, DUB (0_1)
    # second. Anything else the upstream adds later gets appended.
    category_order: list[str] = []
    for key in ("0_0", "0_1"):
        if key in categories:
            category_order.append(key)
    for key in categories:
        if key not in category_order:
            category_order.append(key)
    seasons: list[Season] = []
    for s_idx, cat_id in enumerate(category_order, start=1):
        # Restrict to episodes for this category, group by episode
        # title (each "N серія" appears once per studio). We use the
        # title as the join key so the season has consistent
        # per-studio counts.
        cat_eps_by_studio: dict[str, list[_EpisodeRow]] = {}
        for ep in episodes:
            if _category_id(ep.data_id) != cat_id:
                continue
            cat_eps_by_studio.setdefault(_studio_id(ep.data_id), []).append(ep)
        if not cat_eps_by_studio:
            continue
        # Pick the studio with the most episodes for the season's
        # "canonical" episode list. Other studios are surfaced as
        # per-episode translations.
        canonical_studio = max(
            cat_eps_by_studio.keys(),
            key=lambda sid: len(cat_eps_by_studio[sid]),
        )
        canonical_eps = cat_eps_by_studio[canonical_studio]
        episodes_out: list[Episode] = []
        for e_idx, ep in enumerate(canonical_eps, start=1):
            # Find every studio that has an episode at this position
            # in the season (slice by episode title).
            translations: list[Translation] = []
            for sid, eps in cat_eps_by_studio.items():
                if not any(other.title == ep.title for other in eps):
                    continue
                studio_name = studios.get(sid, sid) or sid
                translations.append(Translation(id=sid, label=studio_name))
            episodes_out.append(
                Episode(
                    number=e_idx,
                    id=f"{external_id}:s{s_idx}e{e_idx}",
                    title=ep.title,
                    translations=translations,
                )
            )
        seasons.append(Season(number=s_idx, episodes=episodes_out))
    return seasons


def _pick_episode_url(
    playlist: dict[str, Any], season_idx: int, episode_idx: int, studio_id: str
) -> str | None:
    """Return the ashdi-or-fallback file URL for the (season, episode,
    studio) triple, or None if the studio lacks an episode at that
    position.

    The upstream Kotlin tries ashdi.vip first, then moonanime, then
    the remaining player. We mirror that priority here."""
    # Find the canonical episode title for (season, episode). The
    # season's canonical studio is the one with the most entries;
    # we don't need that for the lookup — we just need the Nth
    # episode title to match across studios.
    categories = playlist["categories"]
    cat_id_list = ["0_0", "0_1"]
    # Build a stable list of category ids in canonical order.
    cat_ids: list[str] = []
    for key in cat_id_list:
        if key in categories:
            cat_ids.append(key)
    for key in categories:
        if key not in cat_ids:
            cat_ids.append(key)
    if not (1 <= season_idx <= len(cat_ids)):
        return None
    season_cat_id = cat_ids[season_idx - 1]
    # Collect every studio's episode list for this category, ordered
    # by studio_id to be deterministic.
    studio_eps: list[tuple[str, list[_EpisodeRow]]] = []
    for sid in sorted({_studio_id(ep.data_id) for ep in playlist["episodes"]}):
        eps = [
            ep
            for ep in playlist["episodes"]
            if _category_id(ep.data_id) == season_cat_id
            and _studio_id(ep.data_id) == sid
        ]
        if eps:
            studio_eps.append((sid, eps))
    # Match by episode idx: pick the studio that has at least
    # `episode_idx` episodes, then take the episode title from the
    # studio with the most entries (canonical studio).
    if not studio_eps:
        return None
    # The canonical studio is the one with the most episodes — we
    # use its episode titles as the join key for the (studio, ep) match.
    studio_eps.sort(key=lambda kv: -len(kv[1]))
    _, canonical_eps = studio_eps[0]
    if not (1 <= episode_idx <= len(canonical_eps)):
        return None
    target_title = canonical_eps[episode_idx - 1].title
    # Locate the studio_eps entry for the requested studio id.
    target_studio_eps: list[_EpisodeRow] | None = None
    for sid, eps in studio_eps:
        if sid == studio_id:
            target_studio_eps = eps
            break
    if target_studio_eps is None:
        return None
    matching = [ep for ep in target_studio_eps if ep.title == target_title]
    if not matching:
        return None
    # Prefer ashdi.vip first, then moonanime, then anything else.
    return _preferred_player_url(matching)


def _preferred_player_url(eps: list[_EpisodeRow]) -> str | None:
    """Pick the most reliable player URL among the candidates.

    The upstream Kotlin prefers ashdi.vip, then moonanime, then
    peertube. We mirror that priority."""
    ordered = sorted(eps, key=lambda ep: _player_priority(ep.file_url))
    return ordered[0].file_url if ordered else None


def _player_priority(url: str) -> int:
    """Lower priority wins. ashdi.vip = 0 (most reliable), moonanime =
    1, peertube = 2, anything else = 3."""
    if "ashdi.vip" in url:
        return 0
    if "moonanime.art" in url:
        return 1
    if "peertube" in url:
        return 2
    return 3


class AnitubeinuaProvider(BaseProvider):
    id = "anitubeinua"
    name = "Anitubeinua"
    types = ("anime",)
    sections = ANITUBEINUA_SECTIONS
    # v3 (issue #70): "Нові" contributes to «Новинки».
    newest_section = "page"

    async def _get(
        self,
        url: str,
        http: httpx.AsyncClient,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Shared GET helper. `headers` is concatenated with the
        default `Referer` so callers can override per-request."""
        merged = {"Referer": BASE_URL + "/"}
        if headers:
            merged.update(headers)
        try:
            response = await http.get(url, headers=merged)
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        return response

    async def _post(
        self, url: str, data: dict[str, str], http: httpx.AsyncClient
    ) -> httpx.Response:
        try:
            response = await http.post(
                url, data=data, headers={"Referer": BASE_URL + "/"}
            )
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError(
                "upstream_unreachable", f"status {response.status_code}"
            )
        return response

    async def _load_playlist(
        self, external_id: str, http: httpx.AsyncClient
    ) -> dict[str, Any]:
        """Fetch the content page (for the dle_login_hash) and the
        AJAX playlist JSON. Returns the parsed playlist dict.

        The dle_login_hash is read from the inline `<script>` block on
        the content page (matches the upstream Kotlin
        `substringAfterLast("dle_login_hash = '")` extraction)."""
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        content_resp = await self._get(f"{BASE_URL}/{external_id}.html", http)
        hash_ = _extract_dle_hash(content_resp.text)
        news_id = external_id.split("-", 1)[0]
        return await self._fetch_playlist(news_id, external_id, http, hash_)

    async def _fetch_playlist(
        self, news_id: str, external_id: str, http: httpx.AsyncClient, hash_: str
    ) -> dict[str, Any]:
        """GET the AJAX playlist endpoint, parse the JSON envelope, and
        return the parsed playlist dict."""
        ajax_url = (
            f"{BASE_URL}/engine/ajax/playlists.php?news_id={news_id}"
            f"&xfield=playlist&user_hash={hash_}"
        )
        try:
            ajax_resp = await http.get(
                ajax_url,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{BASE_URL}/{external_id}.html",
                },
            )
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if ajax_resp.status_code != 200:
            raise ProviderError("not_found", f"ajax status {ajax_resp.status_code}")
        try:
            payload = cast(dict[str, Any], ajax_resp.json())
        except json.JSONDecodeError as error:
            raise ProviderError("parse_failed", "ajax response not JSON") from error
        if not payload.get("success") or not payload.get("response"):
            raise ProviderError("parse_failed", "ajax response not successful")
        return _parse_playlist(str(payload["response"]))

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # DLE search form posts to the site root with the same
        # fields the upstream Kotlin uses (`do`/`subaction`/`story`).
        # `story` is URL-safe via httpx's form encoder; we do NOT
        # pre-`replace(" ", "+")` because that double-encodes.
        response = await self._post(
            BASE_URL + "/",
            {"do": "search", "subaction": "search", "story": query},
            http,
        )
        soup = BeautifulSoup(response.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select("article.story"):
            parsed = _parse_story(card, self.id)
            if parsed is not None:
                results.append(parsed)
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        # Only one section ("page") — the upstream `mainPage` only
        # ships `/anime/page/`. Anything else is unknown.
        if section != "page":
            raise ProviderError("not_found", f"unknown section: {section}")
        url = f"{BASE_URL}/anime/page/{page}/"
        response = await self._get(url, http)
        soup = BeautifulSoup(response.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select("article.story"):
            parsed = _parse_story(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # DLE pagination: `<div class="navigation">` containing `<span
        # class="lcol navi_pages">` with `<a href=".../page/N/">`
        # siblings. Any link to a higher page number than `page` means
        # there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("div.navigation a[href*='/page/']")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        response = await self._get(f"{BASE_URL}/{external_id}.html", http)
        soup = BeautifulSoup(response.text, "lxml")
        title, poster, description, year = _parse_content_meta(soup)
        # Playlist: fetch the AJAX payload and group episodes into
        # seasons. Defaults to no seasons if the AJAX call fails so
        # the response still has a valid (empty) translation list.
        seasons: list[Season] | None = None
        translations_level: str = "content"
        try:
            playlist = await self._load_playlist(external_id, http)
            seasons = _build_seasons(playlist, external_id)
            if seasons:
                translations_level = "episode"
        except ProviderError as e:
            # Network/server failures must propagate so the health
            # tracker sees the provider as down; only unparseable
            # upstream payloads degrade to an empty season list.
            if e.code in {"unreachable", "upstream_unreachable"}:
                raise
            seasons = None
        # Always have at least one translation so the model min_length
        # check passes.
        translations = [Translation(id="uk", label="Українська")]
        mb_form, mb_styles = model_b_axes("anime")
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            type="anime",
            title=title,
            year=year,
            description=description,
            poster=poster,
            translations=translations,
            form=mb_form,
            styles=mb_styles,
            seasons=seasons,
            translations_level=translations_level,  # type: ignore[arg-type]
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # content_id arrives as either "<external_id>" (bare, from a
        # search result) or "<external_id>:s<N>e<M>" (series episode).
        # /api/stream strips the `<provider>:` prefix before calling us.
        if ":" in content_id:
            ext_id, _, ep_part = content_id.rpartition(":")
        else:
            ext_id, ep_part = content_id, ""
        if not _EXTERNAL_ID_RE.fullmatch(ext_id):
            raise ProviderError("not_found", f"bad external_id: {ext_id!r}")
        m = _EPISODE_RE.fullmatch(ep_part)
        if not m:
            raise ProviderError(
                "parse_failed", f"malformed episode suffix: {ep_part!r}"
            )
        season_idx, episode_idx = int(m.group(1)), int(m.group(2))
        playlist = await self._load_playlist(ext_id, http)
        studio_id = self._resolve_studio_id(playlist, translation, season_idx)
        if studio_id is None:
            raise ProviderError(
                "parse_failed",
                f"translation {translation!r} not in season {season_idx}",
            )
        file_url = _pick_episode_url(playlist, season_idx, episode_idx, studio_id)
        if file_url is None:
            raise ProviderError(
                "parse_failed",
                f"no stream URL for s{season_idx}e{episode_idx}",
            )
        return await self._fetch_m3u8(file_url, http)

    async def _fetch_m3u8(
        self, file_url: str, http: httpx.AsyncClient
    ) -> StreamResponse:
        """GET the iframe URL and extract the m3u8 from the inline
        `file: '...'` script. ashdi.vip embeds use `/embed/`, vod use
        `/vod/`; the upstream Kotlin normalizes the path before
        fetching the m3u8. We just GET the iframe URL as-is."""
        try:
            player_resp = await http.get(
                file_url,
                headers={"Referer": _referer_for(file_url)},
            )
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if player_resp.status_code != 200:
            raise ProviderError("not_found", f"player status {player_resp.status_code}")
        m3u8 = _FILE_RE.search(player_resp.text)
        if not m3u8:
            raise ProviderError("parse_failed", "no m3u8 in player page")
        return StreamResponse(
            url=m3u8.group(1),
            type="m3u8",
            headers={"Referer": _referer_for(file_url), "User-Agent": "cs-uk-api/0.1"},
        )

    @staticmethod
    def _resolve_studio_id(
        playlist: dict[str, Any], translation: str | None, season_idx: int
    ) -> str | None:
        """Translate the user-supplied translation (studio label or
        studio id) to the 3-segment studio id. Returns None when the
        studio is not present in the season — the caller surfaces
        that as `parse_failed`.

        The translation is matched against both the studio label
        (e.g. "FanVoxUA") and the studio id (e.g. "0_1_0") so users
        can pick either form. When `translation` is None, we fall
        back to the canonical (most-episode) studio of the season."""
        studios = playlist["studios"]
        # Restrict to the season's category.
        categories = playlist["categories"]
        cat_id_list = ["0_0", "0_1"]
        cat_ids: list[str] = []
        for key in cat_id_list:
            if key in categories:
                cat_ids.append(key)
        for key in categories:
            if key not in cat_ids:
                cat_ids.append(key)
        if not (1 <= season_idx <= len(cat_ids)):
            return None
        season_cat_id = cat_ids[season_idx - 1]
        # Build the season's studio-id set.
        season_studio_ids = sorted(
            {
                _studio_id(ep.data_id)
                for ep in playlist["episodes"]
                if _category_id(ep.data_id) == season_cat_id
            }
        )
        if not season_studio_ids:
            return None
        if translation is None:
            # Default to the canonical studio (most episodes).
            return max(
                season_studio_ids,
                key=lambda sid: sum(
                    1
                    for ep in playlist["episodes"]
                    if _category_id(ep.data_id) == season_cat_id
                    and _studio_id(ep.data_id) == sid
                ),
            )
        # Direct match (studio id).
        if translation in season_studio_ids:
            return translation
        # Label match (studio name).
        for sid in season_studio_ids:
            if studios.get(sid, "").strip() == translation.strip():
                return sid
        return None

    async def episode_translations(
        self, content_id: str, http: httpx.AsyncClient
    ) -> list[str] | None:
        """Return the studio ids available for a specific episode, or
        None when the episode cannot be resolved. The caller falls back
        to content-level translations in that case."""
        if ":" not in content_id:
            return None
        ext_id, _, ep_part = content_id.rpartition(":")
        if not _EXTERNAL_ID_RE.fullmatch(ext_id):
            return None
        m = _EPISODE_RE.fullmatch(ep_part)
        if not m:
            return None
        season_idx, _ = int(m.group(1)), int(m.group(2))
        try:
            playlist = await self._load_playlist(ext_id, http)
        except ProviderError:
            return None
        categories = playlist["categories"]
        cat_id_list = ["0_0", "0_1"]
        cat_ids: list[str] = []
        for key in cat_id_list:
            if key in categories:
                cat_ids.append(key)
        for key in categories:
            if key not in cat_ids:
                cat_ids.append(key)
        if not (1 <= season_idx <= len(cat_ids)):
            return None
        season_cat_id = cat_ids[season_idx - 1]
        out: list[str] = []
        seen: set[str] = set()
        for ep in playlist["episodes"]:
            if _category_id(ep.data_id) != season_cat_id:
                continue
            sid = _studio_id(ep.data_id)
            if sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
        return out or None


__all__ = ["AnitubeinuaProvider"]
