# Діагностика провайдерів (2026-08-08)

Методологія (грилінг-сесія, узгоджено): статичний розбір кожного адаптера за
чеклістом (контракт vs CONTEXT.md, boundary-валідація, вибір перекладу,
stream-резолюція, кеш, семантика помилок, прогалини в тестах) + жива перевірка
(search → content на живому сайті) там, де середовище дозволяє. Баг = логічний
баг адаптера; upstream drift живе в таблиці як ⚠️, у список багів не потрапляє.
Фіксів коду не вносилося.

**Базова лінія:** `pytest cs_uk_api/tests` — **698 passed, 0 red** (23.7s).
**Середовище:** мережа жива, mpv + chromium доступні, uakino-сесія працює.

---

## Таблиця провайдерів

Легенда: ✅ працює / ⚠️ drift або деградація / 🐛 баг коду / ⛔ недосяжний.
`search:`/`eps:` — результат живого прогону 2026-08-08 (query за замовчуванням
«Дюна», для hentaiukr «школярки», bambooua «квітка», cikavaideya «фільм»).

| id | live search | live content | вердикт | нотатки |
| --- | --- | --- | --- | --- |
| uakino | ✅ 5 | ✅ series, 3 eps | ✅ | headless-Chromium сесія працює; мовчазний Cloudflare обхід тримається |
| ufdub | ✅ 4 | ⚠️ eps: 0 | 🐛 | серіали: 0 епізодів у каталозі; stream завжди перший файл (BUG-3) |
| unimay | ✅ 6 | ✅ movie | ✅ | фільми граються через bare-id fallback клієнта |
| kinotron | ✅ 7 | ✅ movie | 🐛 | **епізоди серіалів → 404** (BUG-1) |
| cikavaideya | ✅ 18 | ✅ 1 ep | ✅ | |
| hentaiukr | ✅ 1 | ✅ 2 eps | ✅ | ⚠️ hevc → ps4-soft-decode-risk (відомо, документовано) |
| bambooua | ✅ 4 | ✅ 8 eps | ✅ | gated → 404 `gated` (документована поведінка) |
| kinovezha | ✅ 1 | ✅ 1 ep | ✅ | |
| animeua | ✅ 1 | ✅ movie | ✅ | |
| uaflix | ✅ 5 | ✅ movie | 🟡 | немає `fullmatch`-валідації slug (обіцяно в status.md) |
| coaninet | ⛔ 404 | — | ⚠️ | `/api/v1/search` → 404 «Page not found» — ендпоінт зник/переїхав |
| eneyida | ✅ 7 | ✅ 1 ep | ✅ | safe_get з allowlist вже застосований |
| klontv | ⛔ 301 | — | ⚠️ | `klon.fun` → **301 → `klonua.com`** — сайт мігрував; пошук не слідує редиректу |
| serialno | ✅ 1 | ⚠️ eps: 0 | 🐛 | живий payload змінив форму → 0 епізодів; file не зрізається (BUG-2) |
| doramyworld | ⚠️ 0 | — | ⚠️ | «Дюна» не дала хітів — невідкласифіковано (query-dependent) |
| uaserialspro | ✅ 8 | ✅ 1 ep | ✅ | AES/PBKDF2 декрипт працює живцем |
| anitubeinua | ✅ 1 | ⚠️ eps: 0 | ⚠️ | AJAX-playlist змінився: 2 блоки замість 4 → епізоди не парсяться |
| simpsonsuatv | ✅ | ⚠️ content TIMEOUT (>30s) | 🟡 | show-сторінка послідовно фетчить кожен сезон (30+ запитів) |
| animeon | ✅ (фільм 7808) | ✅ movie | 🐛 | **фільми непрогравані** (BUG-4) |

Провайдерів без живих проблем і з повним циклом «search → content з епізодами»:
uakino, unimay, cikavaideya, hentaiukr, bambooua, kinovezha, animeua, eneyida,
uaserialspro. kinotron/uaflix/ufdub/unimay/kinovezha/animeua-фільми граються
через bare-id fallback клієнта (`ScreenContent::playEpisode` при відсутніх
сезонах стрімить `id_`).

---

## Список багів (підтверджені логічні баги коду)

### BUG-1 🔴 kinotron — епізоди серіалів завжди 404

**Файл:** `backend/cs_uk_api/providers/kinotron.py`, `stream()`.

**Опис:** `/api/stream` зрізає префікс `kinotron:` (main.py `_split_content_id`),
тому провайдер отримує `ext:sNeM` (2 частини після `split(":")`). Код:
`external_id = parts[-2] if len(parts) >= 3 else parts[-1]` — при 2 частинах
бере `parts[-1]` = `"s1e1"` як external_id → `_SLUG_RE.fullmatch` падає →
`not_found`. Тести маскують це тим, що передають id **з** префіксом
(`test_kinotron_stream_selects_requested_episode` викликає
`stream("kinotron:3663-…:s1e2", …)` — 3 частини, працює), чого в продакшні
не буває.

**Відтворення (live, 2026-08-08):**
```
KinoTronProvider().stream("5615-granchester:s1e1", None, http)
→ ProviderError code='not_found' msg='bad external_id'
```
Фікстура-тест із продакшн-формою (`slug:sNeM`) відтворює той самий результат.

**Вплив:** жоден епізод kinotron-серіалів не програється. Фільми працюють
(bare id). Live-gate 2026-08-01 не зловив, бо гейтив фільм.

---

### BUG-2 🔴 serialno — живі payload не дають епізодів; file-URL не зрізається

**Файл:** `backend/cs_uk_api/providers/serialno.py`, `_load_series_seasons` /
`_select_stream_url`.

**Опис (drift, що ламає код):** код очікує dub-обгортку
`[{"title": "Студія", "folder": [сезони]}]`, живий Tortuga payload —
плоский `[{"title": "Сезон 1", "folder": [епізоди]}]`. `data[0]["folder"]`
інтерпретується як список сезонів, але це епізоди; у них немає `folder` →
0 сезонів. Додатково: живий `file` = `{КІНО}https://…m3u8(subtitle:)`, а
`_select_stream_url` повертає його як є (без зрізання `{label}`/`(subtitle:)`,
як роблять kinovezha та uaserialspro) — навіть після виправлення структури
URL був би битий.

**Відтворення (live):**
```
search "Дюна" → 1398-dyuna → content() → seasons: 0
# decoded payload (tortuga.tw/embed/1400):
#   [{"title":"Сезон 1","number":"1","folder":[{"id":"1-1","title":"Серія 1",
#     "file":"{КІНО}https://calypso.tortuga.tw/…/index.m3u8(subtitle:)", …}]}]
# фікстура (player_embed.html) має СТАРУ форму з dub-обгорткою і чистими URL
```

**Вплив:** серіали serialno видно в каталозі, але без епізодів — непрогравані.
Потрібне оновлення адаптера під нову форму + нові фікстури.

---

### BUG-3 🟠 ufdub — серіали без епізодів; stream ігнорує вибір епізоду

**Файл:** `backend/cs_uk_api/providers/ufdub.py`, `_parse_seasons` / `stream()`.

**Опис:** `_parse_seasons` повертає `[Season(number=1, episodes=[])]` —
порожній список епізодів (у docstring прямо «defer that round-trip»). У
`stream()` немає жодного парсингу епізод-суфікса: `_extract_media_url` бере
перший рядок `var a = [[title, 'mp4', url], …]` — завжди перший файл.
Підсумок: у каталозі серіал без епізодів; натискання «грати» стрімить перший
файл незалежно від вибору.

**Відтворення (live):** `content("anime-23-…")` → `seasons[0].episodes == []`.
**Вплив:** неможливий вибір епізоду/перекладу на серіалах; фільми працюють.

---

### BUG-4 🟠 animeon — фільми непрогравані

**Файл:** `backend/cs_uk_api/providers/animeon.py`, `_movie_content` / `stream()`.

**Опис:** фільми повертають `seasons=None` і жодного episode id. Клієнт у
movie-випадку стрімить bare content id (`animeon:7808`), а `stream()` вимагає
3-частинний id (`id:eN:b64`) → `not_found bad content_id`.

**Відтворення (live, 2026-08-08):**
```
search "фільм" → 7808 «Фільм «Покемон: Сила нас»» → content(): type=movie,
seasons=None → AnimeONProvider().stream("7808", None, http)
→ ProviderError code='not_found' msg='bad content_id'
```
Фікстура `movie_translations.json` показує `episodesCount: 0` — для цього
фільму епізодів немає. Але якщо хоч один фільм має player-епізоди
(`episodesCount > 0`), `_movie_content` їх ігнорує — фільм стає непрограваним
попри наявні дані. Перевірити на живому каталозі.

---

## Знайдені дефекти нижчого рівня (🟡/🔵, не блокери)

### 🟡 D1. `scripts/gate.sh` stale — гейт не працює для жодного провайдера
`gate_one` читає `json.loads(...)['results']`, але `/api/search` тепер віддає
`groups` (issue #71) — KeyError на першому ж запиті. `scripts/gate.sh` не
оновлювався під merged-search контракт; live-gate таблиця в `PROVIDERS.md`
веде в оману.

### 🟡 D2. uakino: фільм без `data-voice` → `translations=[]` → 500
`content()` для movie формує `translations` тільки з елементів з `voice`.
Якщо жоден item не має `data-voice` → порожній список → Pydantic
`min_length=1` → необроблений `ValidationError` → 500 `internal` (не
ProviderError). Латетний — живцем не відтворено.

### 🟡 D3. ufdub / uaflix: `follow_redirects=True` без host-allowlist
У цих двох провайдерах `http.get(..., follow_redirects=True)` на content і
player hop-ах — на противагу нещодавньому рефакторингу `safe_get` (eneyida,
simpsonsuatv, uakino), який фенсить редиректи через allowlist. Постур SSRF
неконсистентний.

### 🟡 D4. uaflix: немає `fullmatch`-валідації external_id
`content()`/`stream()` не валідують slug на межі (status.md: «All providers
apply `re.fullmatch` slug validation»). `_content_url` інтерполює
`external_id` без перевірки — `films/../../../etc/passwd` пройшло б у URL.

### 🟡 D5. anitubeinua: `content()` ковтає `ProviderError` (включно з `unreachable`)
`except ProviderError: seasons = None` ховає і network-збій AJAX — health
tracker фіксує `ok=True`, а користувач бачить 200 з порожніми сезонами.

### 🔵 D6. simpsonsuatv: послідовні фетчі сезонів — content() > 30s (live)
`_build_seasons` для show-сторінки фетчить кожен сезон послідовно. Для
«Сімпсонів» (35 сезонів) — 35 послідовних запитів без бюджету. Живий probe:
content TIMEOUT (>30s). Розпаралелити або обмежити.

### 🔵 D7. `re.match` замість `fullmatch` на episode-суфіксах
anitubeinua (`_EPISODE_RE.match`), cikavaideya (`_select_player_url`),
klontv (`_select_episode_url`) приймають `s1e2garbage` як `s1e2`. Невелика
шорсткість валідації — решта використовують `fullmatch`.

### 🟡 D8. Документація розійшлася з кодом (не баг коду)
- CONTEXT.md документує Model B (form + styles); код живе на Model A
  (`MediaType = movie|series|anime|cartoon|dorama`). Обов'язок ADR-0001
  (осі `form`/`styles` у ключі кешу `/api/search`) **ще не активований** —
  фільтри не зашиплені, колізії кешу немає.
- `SearchResponse.groups` (issue #71) vs CONTEXT.md розділ failure-semantics
  (`results: list[SearchResult]`).
- Live-gate таблиця в `PROVIDERS.md` та вердикти `ready` застаріли (див.
  coaninet/klontv/serialno/anitubeinua вище).

---

## Upstream drift (⚠️, у таблиці — не баги коду)

| Провайдер | Що змінилося | Що потрібно |
| --- | --- | --- |
| coaninet | `/api/v1/search` → 404 | знайти новий ендпоінт; оновити фікстури |
| klontv | `klon.fun` → 301 `klonua.com` | змінити BASE_URL + редирект-політику; нові фікстури |
| serialno | Tortuga payload без dub-обгортки; `{label}`/`(subtitle:)` у file | переписати розбір під нову форму (див. BUG-2) |
| anitubeinua | playlist AJAX: 4 блоки → 2 | переписати `_parse_playlist` під нову розмітку |
| simpsonsuatv | — | TitleMap drift (rik-sanchez) вже відмічено в коді; live не перевірявся через D6 |

---

## Що перевіряти при фіксах

1. Після оновлення будь-якого адаптера — нові живі фікстури + `test_<id>.py`
   із продакшн-формою id (без префікса провайдера у `stream()`).
2. BUG-1: додати тест із `slug:sNeM` (2 частини) — він падає на поточному коді.
3. Оновити `scripts/gate.sh` під `groups`; перегейтити coaninet/klontv після
   зміни ендпоінтів.
