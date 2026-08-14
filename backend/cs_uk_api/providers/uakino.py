from __future__ import annotations

import json
import re
import time
from typing import TypedDict
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..http_client import safe_get
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
from ..uakino_browser import _UA, BASE_URL, UakinoSessionProtocol, get_session
from .base import BaseProvider, ProviderError, model_b_axes

# ashdi.vip serves the playlist page; m3u8 manifests and segment URLs
# stay on the same host. The host allowlist refuses SSRF pivots at the
# Shared-IP boundary — uakino.best is reachable only through the
# headless browser session, never via httpx.
_STREAM_ALLOWED_HOSTS: frozenset[str] = frozenset({"ashdi.vip"})

# Sections exposed by Uakino's new-theme navigation. The /animeukr URL is
# the anime sub-site (Ukrainian-dubbed anime). Section ids are stable;
# titles are user-facing and may change.
UAKINO_SECTIONS: tuple[Section, ...] = (
    Section(id="filmy", title="Фільми", form="movie"),
    Section(id="serials", title="Серіали", form="series"),
    Section(id="animeukr", title="Аніме", form="series"),
    Section(id="cartoons", title="Мультфільми", form="movie"),
)

# external_id is "<section>:<id>-<slug>" (e.g. "filmy:12567-dyuna").
# The section segment comes straight from the listing link so content()
# can rebuild the genre-less content URL without a genre->section map.
# We gate both content() and stream() against the charset so a
# caller-supplied value cannot inject "../" into the URL path.
_CONTENT_ID_RE = re.compile(r"^([a-z0-9_-]+):(\d+)-([a-z0-9][a-z0-9-]*)(:__movie__)?$")
# Legacy cache entries looked like "film-dune-2021"; keep accepting the
# kind-prefixed form by mapping film->filmy, serial->seriesss.
_LEGACY_ID_RE = re.compile(r"^(film|serial)-(\d+)-([a-z0-9][a-z0-9-]*)$")
# stream() episode ids from the series playlists: "<news_id>:e<number>".
_EPISODE_RE = re.compile(r"^(\d+):e(\d+)$")

# Sections that appear in search results but have no playable content.
_SKIP_SECTIONS = frozenset(
    {"news", "franchise", "anonsi", "find", "year", "tag", "genre", "page", "ua"}
)

# Section titles the upstream prepends to the Жанр row (a section
# is not a genre — must be filtered out of ContentResponse.genres).
_SECTION_TITLES: frozenset[str] = frozenset(
    s.title for s in UAKINO_SECTIONS
)

# Stream pages on the ashdi.vip CDN embed the playlist URL as
# `file: "https://ashdi.vip/video02/.../index.m3u8"` inside a <script>.
_FILE_RE = re.compile(r"""file\s*:\s*["']([^"']+)["']""")
_SERIES_EP_RE = re.compile(r"^Серія\s+(\d+)$")

_CDN_REFERER = "https://ashdi.vip/"
_DESKTOP_UA = _UA

# MediaType hints for search results, keyed on the section in the href.
_SECTION_TYPES: dict[str, str] = {
    "filmy": "movie",
    "seriesss": "series",
    "animeukr": "anime",
    "cartoon": "cartoon",
}

_CARD_SELECTOR = "div.movie-item.short-item"


class _PlaylistItem(TypedDict):
    file: str
    voice: str | None
    episode: int | None


def _section_url(section: str, page: int) -> str:
    path = {
        "filmy": "/filmy",
        "serials": "/seriesss",
        "animeukr": "/animeukr",
        "cartoons": "/cartoon",
    }.get(section)
    if path is None:
        raise ProviderError("not_found", f"unknown section: {section}")
    if page <= 1:
        return f"{path}/"
    return f"{path}/page/{page}/"


def _parse_year(text: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", text)
    return int(m.group(0)) if m else None


def _parse_cards(html: str) -> list[SearchResult]:
    """Parse new-theme listing/search cards into search results."""
    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []
    for card in soup.select(_CARD_SELECTOR):
        a = card.select_one("a.movie-title")
        if a is None or a.get("href") is None:
            continue
        href = str(a["href"]).strip()
        m = re.search(r"/([a-z0-9_-]+)/(\d+)-([a-z0-9][a-z0-9-]*)\.html$", href)
        if m is None or m.group(1) in _SKIP_SECTIONS:
            continue
        section, item_id, slug = m.group(1), m.group(2), m.group(3)
        title = a.get_text(strip=True)
        img = card.select_one("div.movie-img img")
        poster = urljoin(BASE_URL, str(img["src"])) if img and img.get("src") else None
        deck = card.select("div.movie-desk-item")
        year: int | None = None
        for row in deck:
            label = row.select_one("div.fi-label")
            value = row.select_one("div.deck-value")
            if label is not None and value is not None and "Рік виходу" in label.get_text():
                year = _parse_year(value.get_text())
                break
        if year is None:
            year = _parse_year(title)
        kind = _SECTION_TYPES.get(section)
        if kind is None:
            # Genre segments like drama_series, detective_series,
            # anime-series, cartoonseries all denote serialized content.
            kind = "series" if section.endswith("series") else "movie"
        # The cartoons section («Мультфільми») is animated *films* — its
        # items must be form=movie, not model_b_axes's default series for
        # the style-tagged "cartoon" type (else ?form=movie&style=cartoon
        # search and the section filter both miss them).
        mb_form, mb_styles = model_b_axes(
            kind,  # type: ignore[arg-type]
            form="movie" if section == "cartoon" else None,
        )
        results.append(
            SearchResult(
                id=f"uakino:{section}:{item_id}-{slug}",
                provider="uakino",
                title=title,
                year=year,
                poster=poster,
                url=urljoin(BASE_URL, href),
                form=mb_form,
                styles=mb_styles,
            )
        )
    return results


def _parse_external_id(external_id: str) -> tuple[str, str, str]:
    """Return (section, id, slug) for a validated external_id."""
    m = _CONTENT_ID_RE.fullmatch(external_id)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _LEGACY_ID_RE.fullmatch(external_id)
    if m:
        kind, item_id, slug = m.group(1), m.group(2), m.group(3)
        return ("filmy" if kind == "film" else "seriesss"), item_id, slug
    raise ProviderError("not_found", "bad external_id")


def _normalize_cdn_url(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("//"):
        return "https:" + raw
    return raw


def _direct_player_url(soup: BeautifulSoup) -> str | None:
    """The movie's single playable player iframe URL, or None.

    uakino migrated movie pages off playlists.php (which now answers
    ERR_NOT_DATA for them) to a plain direct player iframe
    (``<iframe src="https://ashdi.vip/vod/<id>">``). The page may also
    embed a youtube trailer — never the player. Returns the bare vod
    URL (query params like ``?geoblock=ua&`` stripped).
    """
    for iframe in soup.select("iframe"):
        src = str(iframe.get("src") or "").strip()
        if "ashdi.vip/vod/" not in src:
            continue
        return src.split("?", 1)[0]
    return None


def _parse_playlists(html: str) -> tuple[list[_PlaylistItem], bool]:
    """Parse the playlists.php response.

    Returns (items, is_series). Movie items carry data-file + data-voice
    directly; series responses have a voice-group header li (no data-file)
    followed by episode li whose text is "Серія N".
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[_PlaylistItem] = []
    is_series = False
    for li in soup.select("div.playlists-videos li"):
        if "voice_crating" in (li.get("class") or []):
            continue
        data_file = li.get("data-file")
        voice_attr = li.get("data-voice")
        voice = str(voice_attr) if voice_attr is not None else None
        text = li.get_text(strip=True)
        ep = _SERIES_EP_RE.match(text)
        if data_file is None:
            continue
        if ep is not None:
            is_series = True
            items.append(
                {
                    "file": _normalize_cdn_url(str(data_file)),
                    "voice": voice,
                    "episode": int(ep.group(1)),
                }
            )
        else:
            items.append(
                {"file": _normalize_cdn_url(str(data_file)), "voice": voice, "episode": None}
            )
    return items, is_series


def _pick_voice(
    candidates: list[_PlaylistItem], translation: str | None
) -> _PlaylistItem | None:
    """Pick a playlist item: the requested voice, or the first one when no
    voice was requested (specified-but-unmatched voices return None so the
    caller can raise translation_missing).

    The synthetic ``"uk"`` default (issue #123: a movie whose playlist
    rows carry no ``data-voice`` surfaces ``Translation(id="uk")`` so
    the model's min_length=1 holds) is a placeholder, not a real studio:
    when a client streams with ``?translation=uk`` and no item carries
    that voice, fall back to the first playable item instead of
    surfacing translation_missing."""
    if translation is not None:
        match = next(
            (it for it in candidates if it.get("voice") == translation), None
        )
        if match is not None:
            return match
        if translation == "uk":
            return candidates[0] if candidates else None
        return None
    return candidates[0] if candidates else None


class UakinoProvider(BaseProvider):
    id = "uakino"
    name = "Uakino"
    types = ("movie", "series", "anime", "cartoon")
    sections = UAKINO_SECTIONS

    def __init__(self, session: UakinoSessionProtocol | None = None) -> None:
        self._session = session

    @property
    def session(self) -> UakinoSessionProtocol:
        if self._session is None:
            self._session = get_session()
        return self._session

    async def _fetch(self, path: str, method: str = "GET", data: str | None = None) -> str:
        try:
            status, text = await self.session.fetch(path, method=method, data=data)
        except Exception as e:
            raise ProviderError("unreachable", str(e)) from e
        if status != 200:
            raise ProviderError("upstream_unreachable", f"status {status}")
        return text

    async def _fetch_playlists(self, news_id: str) -> tuple[list[_PlaylistItem], bool]:
        html = await self._fetch(
            f"/engine/ajax/playlists.php?news_id={quote(news_id)}"
            f"&xfield=playlist&time={int(time.time())}"
        )
        try:
            payload = json.loads(html)
        except json.JSONDecodeError as e:
            raise ProviderError("parse_failed", "playlists response is not JSON") from e
        return _parse_playlists(str(payload.get("response", "")))

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        data = urlencode(
            {
                "do": "search",
                "subaction": "search",
                "story": query,
                "search_start": "0",
                "full_search": "0",
            }
        )
        html = await self._fetch("/index.php", method="POST", data=data)
        return _parse_cards(html)

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        section, item_id, slug = _parse_external_id(external_id)
        html = await self._fetch(f"/{section}/{item_id}-{slug}.html")
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one("h1 span.solototle")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        title = title_el.get_text(strip=True)

        poster_el = soup.select_one("div.film-poster img")
        poster = (
            urljoin(BASE_URL, str(poster_el["src"])) if poster_el and poster_el.get("src") else None
        )
        desc_el = soup.select_one("div.full-text[itemprop=description]")
        description = desc_el.get_text(strip=True) if desc_el else ""

        year: int | None = None
        tags: list[str] = []
        country: str | None = None
        rating: float | None = None
        people: list[Person] = []
        # The upstream template renamed these rows from ``fi-item`` to
        # ``fi-item-s`` (seen live on anime-series pages, August 2026);
        # match both spellings so year/genres/rating/people keep parsing
        # on either template version.
        for item in soup.select("div.fi-item, div.fi-item-s"):
            label = item.select_one("div.fi-label")
            value = item.select_one("div.fi-desc")
            if label is None or value is None:
                continue
            label_text = label.get_text(strip=True)
            if not label_text:
                # The label-less fi-item carries the IMDb-style
                # `<score>/<vote-count>` rating (e.g. 8.0/1118360).
                m = re.match(r"(\d+(?:\.\d+)?)/\d+", value.get_text(strip=True))
                if m:
                    try:
                        rating = float(m.group(1))
                    except ValueError:
                        rating = None
                continue
            if "Рік виходу" in label_text:
                year = _parse_year(value.get_text())
            elif label_text.startswith("Жанр"):
                # The upstream row opens with the SECTION name
                # (e.g. `Серіали , Драма , Пригоди , Фантастика` on a
                # series page) — a section is not a genre, drop it.
                tags = [
                    t.strip()
                    for t in value.get_text().split(",")
                    if t.strip() and t.strip() not in _SECTION_TITLES
                ]
            elif "Країна" in label_text:
                links = value.select("a")
                raw = links[0].get_text(strip=True) if links else value.get_text(strip=True)
                country = " ".join(raw.lower().split()) if raw else None
            elif label_text.startswith("Режисер"):
                for a in value.select("a"):
                    name = a.get_text(strip=True)
                    if name:
                        people.append(
                            Person(id=f"uakino:{name}", name=name, role="Director")
                        )
            elif label_text.startswith("Актори"):
                for a in value.select("a"):
                    name = a.get_text(strip=True)
                    if name:
                        people.append(
                            Person(id=f"uakino:{name}", name=name, role="Actor")
                        )

        ajax_el = soup.select_one("div.playlists-ajax")
        news_id = (
            str(ajax_el.get("data-news_id")) if ajax_el and ajax_el.get("data-news_id") else item_id
        )
        items, is_series = await self._fetch_playlists(news_id)

        if is_series:
            episodes_by_number: dict[int, Episode] = {}
            voices: list[str] = []
            for it in items:
                episode = it["episode"]
                if episode is None:
                    continue
                voice = it.get("voice")
                if voice and voice not in voices:
                    voices.append(voice)
                ep = episodes_by_number.get(episode)
                if ep is None:
                    ep = Episode(
                        number=episode,
                        id=f"uakino:{news_id}:e{episode}",
                        title=f"Серія {episode}",
                        translations=[],
                    )
                    episodes_by_number[episode] = ep
                if voice and voice not in [t.id for t in (ep.translations or [])]:
                    ep.translations = [*(ep.translations or []), Translation(id=voice, label=voice)]
            if not episodes_by_number:
                raise ProviderError("parse_failed", "series playlists has no episodes")
            seasons = [
                Season(
                    number=1,
                    episodes=[episodes_by_number[n] for n in sorted(episodes_by_number)],
                )
            ]
            translations = [Translation(id=v, label=v) for v in voices]
            # animeukr section = anime series: keep the anime style on
            # content so it matches the item's search/browse card
            # (styles=["anime"]), not a plain series with no style.
            mb_form, mb_styles = model_b_axes(
                "anime" if section == "animeukr" else "series"
            )
            return ContentResponse(
                id=f"uakino:{external_id}",
                title=title,
                year=year,
                description=description,
                poster=poster,
                genres=tags,
                people=people,
                rating=rating,
                translations=translations,
                seasons=seasons,
                translations_level="episode",
                form=mb_form,
                styles=mb_styles,
                country=country,
            )

        translations = [
            Translation(id=str(it["voice"]), label=str(it["voice"]))
            for it in items
            if it.get("voice")
        ]
        # A movie whose playlist rows carry no voice label (single
        # direct-file entry) must still surface a playable default —
        # the model requires at least one translation (issue #123, D2).
        if not translations:
            translations = [Translation(id="uk", label="Українська")]
        # A movie whose playlist response is empty (upstream moved movie
        # pages off playlists.php to a direct player iframe) is only
        # playable when the page carries that iframe. A movie with
        # neither playlists nor a direct player is a dead card — gate it
        # (ADR-0002) so the catalog sweep drops it instead of failing at
        # play time.
        if not items and _direct_player_url(soup) is None:
            raise ProviderError("gated", "no playable source on movie page")
        movie_type = "anime" if "аніме" in " ".join(tags).lower() else "movie"
        # A style-tagged movie (аніме-фільм) is form=movie, not series
        # (model_b_axes defaults style-tagged types to series).
        mb_form, mb_styles = model_b_axes(movie_type, form="movie")  # type: ignore[arg-type]
        return ContentResponse(
            id=f"uakino:{external_id}",
            title=title,
            year=year,
            description=description,
            poster=poster,
            genres=tags,
            people=people,
            rating=rating,
            translations=translations,
            seasons=None,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        news_id: str
        episode: int | None
        m = _EPISODE_RE.fullmatch(content_id)
        if m:
            news_id, episode = m.group(1), int(m.group(2))
        else:
            _, item_id, _ = _parse_external_id(content_id)
            news_id, episode = item_id, None

        items, _ = await self._fetch_playlists(news_id)
        if episode is not None:
            candidates = [it for it in items if it.get("episode") == episode]
            if not candidates:
                raise ProviderError("not_found", f"episode {episode} not found in playlists")
            chosen = _pick_voice(candidates, translation)
            if chosen is None:
                raise ProviderError(
                    "translation_missing",
                    f"voice {translation!r} not found in playlists",
                )
            stream_page_url = str(chosen["file"])
        else:
            candidates = [it for it in items if it.get("episode") is None]
            if candidates:
                chosen = _pick_voice(candidates, translation)
                if chosen is None:
                    raise ProviderError(
                        "translation_missing",
                        f"voice {translation!r} not found in playlists",
                    )
                stream_page_url = str(chosen["file"])
            else:
                # Movie with no playlists data: upstream moved movie
                # pages off playlists.php to a direct player iframe.
                # Resolve the player URL from the content page.
                section, item_id, slug = _parse_external_id(content_id)
                html = await self._fetch(f"/{section}/{item_id}-{slug}.html")
                direct_url = _direct_player_url(BeautifulSoup(html, "lxml"))
                if direct_url is None:
                    raise ProviderError(
                        "parse_failed", "no playable voices in playlists"
                    )
                stream_page_url = direct_url
        try:
            resp = await safe_get(
                http,
                stream_page_url,
                allowed_hosts=set(_STREAM_ALLOWED_HOSTS),
                headers={
                    "User-Agent": _DESKTOP_UA,
                    "Referer": f"{BASE_URL}/",
                    "Accept-Language": "uk-UA,uk;q=0.9",
                },
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.url.host not in _STREAM_ALLOWED_HOSTS:
            raise ProviderError("not_found", "unexpected upstream host")
        if resp.status_code != 200:
            raise ProviderError("not_found", f"stream page status {resp.status_code}")

        m3u8_url = next(
            (u for u in _FILE_RE.findall(resp.text) if u.endswith(".m3u8")),
            None,
        )
        if m3u8_url is None:
            raise ProviderError("parse_failed", "no m3u8 link in stream page")
        # The m3u8 URL is content we hand to the LAN client. Reject
        # anything outside the ashdi.vip host so a hostile stream page
        # can't redirect the PS4 into an internal address.
        if urlparse(m3u8_url).netloc not in _STREAM_ALLOWED_HOSTS:
            raise ProviderError("not_found", "m3u8 host not in allowlist")
        # The stream CDN (ashdi.vip) is served over plain HTTP and needs
        # the Referer to authorize the manifest/segment fetches — but a
        # comma-bearing desktop UA breaks clients that split headers on
        # commas (e.g. mpv's --http-header-fields): the CDN then sees a
        # malformed User-Agent and 400s the stream. Use the plain client
        # UA like every other ashdi-backed provider.
        return StreamResponse(
            url=m3u8_url,
            type="m3u8",
            headers={"Referer": _CDN_REFERER, "User-Agent": "cs-uk-api/1.0"},
        )

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        html = await self._fetch(_section_url(section, page))
        # Host-fence the pagination discovery: only `/page/N/` anchors
        # that point at uakino.best count as next-page evidence. The
        # raw `href` regex would match cross-site links (e.g. a
        # commenter's signature linking to `evil.com/page/2/`) and
        # incorrectly mark `has_next=True`.
        soup = BeautifulSoup(html, "lxml")
        base_host = urlparse(BASE_URL).netloc
        has_next = False
        for a in soup.select("a[href*='/page/']"):
            href = str(a.get("href") or "")
            if urlparse(href).netloc not in (base_host, ""):
                parsed = urlparse(urljoin(BASE_URL, href))
                if parsed.netloc != base_host:
                    continue
            m = re.search(r"/page/(\d+)/", href)
            if m and int(m.group(1)) > page:
                has_next = True
                break
        return _parse_cards(html), has_next
