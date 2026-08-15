# Аудит бекенду проти FastAPI Patterns (2026-08-15)

**Репозиторій:** `ps4-uk-stream` · **Гілка:** `master` · **HEAD:** `e4d1b65`

Аудит структури `backend/cs_uk_api` на відповідність патернам
FastAPI: layout, DI, сервісний шар, конфігурація, lifespan, тести.
Еталон — skill `fastapi-patterns`.

## 1. Layout — частково відповідає

| Skill-патерн | Стан проєкту | Вердикт |
|---|---|---|
| `routers/` директорія | Лише jellyfin має власний router (`jellyfin/router.py:111`); усі нативні `/api/*` маршрути лежать інлайн у `main.py:292-721` (721 рядок) | ⚠️ відхилення |
| `schemas/` vs `models/` | Єдиний `models.py` (424 рядки, 19 BaseModel) — wire-моделі, не розділені на request/response | ⚠️ частково |
| `services/` | `service.py` тонкий (тільки `upstream_guard` + перекладачі помилок); основна бізнес-логіка — `catalog.py` + `catalog_state/` (spec #309) | ✅ за духом |
| `database.py` | Немає БД — in-memory + файлові стори (`resume_store.py`, `user_state.py`, `snapshot_store.py`). SQLAlchemy-патерни N/A | ✅ N/A |

**Головне відхилення:** `main.py` досі тримає ~20 нативних маршрутів
інлайн. Маршрути стали тонкими (вся робота пішла за `catalog.*` seam
після spec #309), але файлова організація лишилась. Кандидат: винести
нативні маршрути в `routers/native.py`.

## 2. DI — мінімальний, але виправданий

- `Depends()` використовується лише для `require_token` у
  jellyfin-фасаді (`jellyfin/router.py:961+`).
- Нативні маршрути беруть глобальні синглтони напряму: `TRACKER`,
  `PROVIDERS`, `get_client()`, `get_session()`, `SETTINGS`.
- Аліасів `Annotated[..., Depends()]` немає; OAuth2PasswordBearer не
  потрібен (auth = accept-any + фіксований токен, ADR-0002).

⚠️ **Відхилення від skill:** немає `dependency_overrides` для тестів —
тести ізолюються env-змінними в `tests/conftest.py:12-30` та respx.
Для поточного дизайну (без БД, без per-request контексту) це розумно,
але це свідомий відступ від патерну.

## 3. Service layer — напрямок вірний, стиль інший

- ✅ Тонкі маршрути: `main.py:549` делегує в
  `catalog.provider_content(...)`; помилки перекладаються через
  `exc_handler` у `service.py:88-104`.
- ✅ Одна спільна точка health-запису + 502 (`upstream_guard`,
  `service.py:44`).
- ⚠️ Немає класів-сервісів типу `UserService(db)` — функціональний
  стиль замість транзакційного. Без БД це коректно.

## 4. Конфігурація — найбільший структурний відступ

`config.py` — рукописний frozen dataclass + ручний `os.environ.get` з
`int()`/`float()` примусами (219 рядків). Skill радить
pydantic-settings.

⚠️ Міграція дала б валідацію (зараз `int("abc")` у env просто крашить
імпорт), але це churn без функціонального виграшу — низький пріоритет.

## 5. App factory / lifespan — сильна сторона

- ❌ Немає `create_app()` фабрики (модульний `app = FastAPI(...)` у
  `main.py:248`).
- ✅ Lifespan зразковий: 4 фонові таски (warm, watchdog, catalog-warm,
  LLM) з коректним cancel+drain та фінальним `flush_playback()` /
  `close_client()` (`main.py:184-245`).

## 6. Тести — 1306 passed, але є гігієнічна проблема

- ⚠️ **pytest-randomly 4.1.0 встановлено глобально, але НЕ в
  `pyproject.toml`** — джерело порядкової флейкі: спостерігали 3, 4 і
  5 різних падінь на різних сідах; `-p no:randomly` → 1306/1306
  зелених. Тобто в suite є state-leak між тестами, який маскується
  порядком.
- ⚠️ `tests/test_api.py:6-8` — модульний `client = TestClient(app)`;
  без context manager lifespan не запускається → фонові таски не
  ізольовані, але й не тестуються; частина тестів, що чіпає глобальний
  стан (`TRACKER`, `PROVIDERS`, кеші), — ймовірне джерело leak.
- ✅ Ізоляція через env у conftest — акуратно задокументовано.
- ✅ 1306 тестів, respx-фікстури per provider — покриття сильне.

## Пріоритети дій

1. **Високий:** знайти/полагодити state-leak, що ламає suite під
   pytest-randomly (або додати `pytest-randomly` у dev-deps і
   полагодити тести під нього).
2. **Середній:** винести нативні маршрути з `main.py` в `routers/`.
3. **Низький:** pydantic-settings для `config.py`; `create_app()`
   фабрика.
