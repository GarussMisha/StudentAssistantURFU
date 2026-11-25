# 03. Исследование: сквозные контракты

- Статус: СОГЛАСОВАНО (v1.0)
- Дата: 2026-11-25
- Зависит от: 01 (web-interface), 02 (product-and-users)
- Питает: 04 (БЗ), 05 (RAG), 06 (чаты/оценки), 07 (аккаунты), 08 (eval), 09 (деплой)
- Закрывает: OQ-01, D-03, D-04, PR-15, OQ-04, OQ-05

## 1. Контекст и цель

Документ фиксирует единый контракт между сервером, веб-интерфейсом и БД: модель
данных, HTTP-эндпоинты, формат SSE, статусы сообщений, коды ошибок, `/health`,
переменные окружения и правила валидации. Всё, что ниже, — обязательство: UI (01)
и RAG (05) реализуются строго по этим формам, а не «по договорённости».

Принцип: **PostgreSQL — источник истины**, Qdrant — производный индекс. Контракт
цитат хранит снимок метаданных в сообщении, поэтому переиндексация корпуса не
меняет историю.

## 2. Факты из кодовой базы

- `openspec/specs` и `openspec/changes` пусты — контракты создаются с нуля,
  совместимость с существующими спеками не требуется.
- `app/core/config.py`: есть `LLM_PROVIDER`, `EMBEDDING_PROVIDER`, `LLM_*`,
  `EMBEDDING_*`, `KB_DATA_DIR`, `SESSION_TTL_DAYS`, `RATE_LIMIT_PER_HOUR`,
  `ADMIN_EMAIL`, `ADMIN_PASSWORD`. Нет: `LLM_MAX_CONCURRENCY`, таймаутов,
  `RETRIEVAL_TOP_K`, `CHAT_MAX_MESSAGES`, `QDRANT_COLLECTION`, `DEBUG`.
- `.env.example`: `LLM_BASE_URL=…/v1`, но `EMBEDDING_BASE_URL=…/v1/embeddings` —
  OpenAI-совместимый клиент добавит `/embeddings` ещё раз. `EMBEDDING_API_KEY`
  отсутствует, хотя OpenRouter для эмбеддингов требует ключ.
- `docker-compose.yml`: healthcheck только у `postgres`/`qdrant`; у `app` его нет.
  `deploy/Caddyfile` в репозитории отсутствует (зона 09).
- `pyproject.toml`: `qdrant-client[fastembed]` — эмбеддинги внешние, extra не нужен.
- `README.md` противоречит 01/02: «честный отказ» вместо `ungrounded`, eval
  «30 вопросов / ≥80%» вместо 20+20 и порогов 19/16. Источник истины —
  `docs/research/` (CC-10), README правится отдельной задачей.
- Схем БД, роутов и Pydantic-моделей нет — документ их и определяет.

## 3. Участники и их контракты

| Участник | Что потребляет | Что обязан соблюдать |
|---|---|---|
| Веб-интерфейс (Jinja2+HTMX+JS) | HTML-страницы, JSON API, SSE | CSRF во всех изменяющих запросах, экранирование, `credential`-cookie |
| RAG-пайплайн (05) | `POST /api/messages` | отдавать `delta`/`sources`/`done` строго по формату, статус из enum |
| БД (04/06/07) | таблицы из §5 | каскады, `CHECK`-констрейнты, UTC |
| Оценка качества (08) | контракт сообщений/статусов | прогон через тот же пайплайн, без обхода API |
| Деплой (09) | `/health/live`, `/health` | healthcheck только на `/health/live` |

## 4. Функциональные требования (контракты)

### FR-1. Транспорт и общие правила API

- JSON API живёт под `/api`, **без версии** (CC-07). Формат — `application/json`,
  имена полей `snake_case`, время — ISO-8601 UTC с `Z`.
- Все `/api/*`, кроме `/health*`, требуют валидной сессии; иначе **401 JSON**
  `unauthorized` (без редиректа). Страницы при отсутствии сессии → `303` на
  `/auth?next=<path>`.
- Изменяющие методы (`POST`, `PUT`, `DELETE`) требуют CSRF-токен: заголовок
  `X-CSRF-Token` или поле `csrf_token`. Токен привязан к серверной сессии
  (CC-30), несовпадение → `403 csrf_invalid`.
- Конверт ошибки:

```json
{"error": {"code": "validation_error", "message": "Текст для пользователя",
           "details": {"field": "причина", "request_id": "…"}}}
```

- `request_id` генерируется middleware (или берётся из `X-Request-ID`), отдаётся
  в заголовке `X-Request-ID` и в `error.details.request_id`.
- `/docs` и `/openapi.json` доступны только при `DEBUG=true`.

### FR-2. Аутентификация (форматная, HTML)

Парные JSON-двойники не создаются (CC-17): вход/регистрация только формами.

| Метод | Путь | Тело | Успех | Ошибки |
|---|---|---|---|---|
| GET | `/auth` | — | 200 страница; при сессии → 303 `/` | — |
| POST | `/auth/register` | `email, password, password_confirm, consent=on` | 303 `/` (+`HX-Redirect`) | 200 + фрагмент с ошибками под полями |
| POST | `/auth/login` | `email, password, next?` | 303 `next` или `/` | 200 + фрагмент, общее сообщение |
| POST | `/auth/logout` | — | 303 `/auth`, строка сессии удалена | 403 `csrf_invalid` |

- Регистрация без `consent` не проходит (D-01). Дубликат email → ошибка под полем.
- Пароль 8…128, email ≤ 254 и нормализуется в lower-case.
- Успешная регистрация сразу создаёт сессию и `consent_at`/`consent_version`.
- Смена пароля (`/api/profile/password`) удаляет все сессии пользователя, **кроме
  текущей** (UI-19).

### FR-3. `POST /api/messages` — единственная точка создания чата и ответа

Отдельного создания чата нет (CC-27): чат рождается первым непустым вопросом.

Запрос:

```json
{"chat_id": null, "text": "Как составить ТЗ?"}
```

- `chat_id = null` → в одной транзакции создаются чат (title = первые 60 символов
  вопроса, обрезка по слову + `…`) и user-сообщение; далее SSE.
- `chat_id` задан → проверяется владелец (иначе `404 not_found`), лимит 40
  сообщений (иначе `409 chat_limit_reached`), отсутствие активного стрима в чате
  (иначе `409 generation_in_progress`).
- `text` после `strip()` непустой, ≤ 2000 символов, иначе `422 validation_error`.
- Ответ всегда `text/event-stream`; **до** первого байта стрима возможны только
  HTTP-ошибки. Их перечень: 401, 403, 404, 409, 422, 503 `llm_busy` (буфер
  очереди заполнен).
- Если запрос отклонён до старта стрима, **user-сообщение не сохраняется**,
  текст остаётся в поле ввода (UI показывает toast).
- Очередь LLM (CC-03): 4 слота, буфер `LLM_QUEUE_MAX=8`, ожидание
  `LLM_QUEUE_TIMEOUT_SECONDS=60`. Нет свободного слота и буфер полон →
  мгновенный `503 llm_busy` (без ожидания). Ожидание истекло уже внутри
  стрима → событие `error {code:"llm_busy"}`.

### FR-4. Формат SSE

Заголовки: `Content-Type: text/event-stream; charset=utf-8`,
`Cache-Control: no-store`, `X-Accel-Buffering: no`. Каждые 15 с — комментарий
`: ping`. Клиент **обязан** игнорировать неизвестные события и поля.

| Событие | Когда | `data` |
|---|---|---|
| `meta` | первым, всегда | `{chat_id, chat_title, user_message_id, message_id, seq}` |
| `delta` | по мере генерации, 0…n раз | `{text}` — инкрементальный фрагмент |
| `sources` | один раз, до `done`, если есть цитаты | `{items: [Source]}` |
| `done` | терминальное при успехе/остановке | `{status, message_id, sources_count}` |
| `error` | терминальное при сбое | `{code, message, retryable}` |

`Source`: `{source_no, chunk_id, document_title, material_type, heading_path,
snippet}`.

Правила:

- `done` и `error` взаимоисключающи и завершают поток; после них сервер закрывает
  соединение.
- `status ∈ grounded | ungrounded | no_answer | stopped`; при `stopped` —
  `sources: []`.
- `source_no` — 1…n в порядке первого упоминания в тексте, максимум 8; `snippet`
  ≤ 400 символов, обрезка по границе слова с `…`.
- `chunk_id` — строка, идентификатор чанка из индекса (формат определяет 04).
- Транспорт: `POST` + `fetch`/`ReadableStream` (EventSource не умеет POST,
  HTMX SSE — только GET). Разрыв соединения клиентом = отмена генерации.
- Обрыв **без** `stop` → частичный текст не сохраняется, статус `no_reply`.

### FR-5. Остановка генерации (новая фича, UI-25)

`POST /api/chats/{id}/stop` → **204** всегда, если чат существует и принадлежит
пользователю (идемпотентно; нет активной генерации — тоже 204). Чужой/несуществующий
чат → `404 not_found`.

- Отмена кооперативная: пайплайн проверяет флаг между чанками.
- Уже сгенерированный текст **сохраняется** со статусом `stopped` (CC-21); если
  не успело прийти ни одного `delta`, сохраняется пустое assistant-сообщение
  `stopped` — история показывает «Ответ остановлен», диалог не выглядит оборванным.
- Стрим завершается `done {status:"stopped", message_id, sources_count:0}`.

### FR-6. История

`GET /api/chats?limit=15&cursor=<opaque>` — только чаты с сообщениями (пустых не
бывает по конструкции). Сортировка по `updated_at DESC, id DESC`.
Ответ:

```json
{"items": [{"id": 12, "title": "Как составить ТЗ?", "created_at": "…",
            "updated_at": "…", "messages_count": 6}],
 "next_cursor": "eyJ1Ijoi…"}
```

`cursor` — `base64url(JSON {"u": updated_at, "i": id})`, keyset-пагинация;
невалидный курсор → `422 validation_error`. `next_cursor = null`, если элементов
меньше `limit`. `limit` 1…50, по умолчанию 15.

`GET /api/chats/{id}/messages?limit=20&before_seq=<seq>` — сообщения **старше**
`before_seq`, по возрастанию `seq` в ответе:

```json
{"items": [Message], "has_more": true, "oldest_seq": 4}
```

- Без `before_seq` — последние `limit`. UI первым запросом просит `limit=2`
  (CC-24), при прокрутке вверх — по 20.
- `limit` 1…40, по умолчанию 20.

`Message`:

```json
{"id": 41, "seq": 5, "role": "assistant", "content_md": "…",
 "status": "grounded",
 "sources": [Source],
 "rating": {"value": "like", "comment": "…", "updated_at": "…"},
 "created_at": "…"}
```

У `role = user`: `status = null`, `sources = []`, `rating = null`.

### FR-7. Оценки

`PUT /api/messages/{id}/rating` (идемпотентный upsert):

```json
{"value": "like", "comment": "Помогло"}
```

- `value ∈ like | dislike | null`; `null` удаляет оценку вместе с комментарием.
- `comment` ≤ 1000, опционален; без оценки невозможен.
- Оценивать можно только assistant-сообщения своих чатов; иначе `404 not_found`.
- Ответ 200: `{value, comment, updated_at}` (+ `value: null` после удаления).

### FR-8. Профиль и удаление истории

- `GET /profile` — HTML: аватар-буква, email, `created_at`, число чатов,
  «Согласие получено <дата>».
- `POST /api/profile/password` `{current_password, new_password}` → 204.
  Неверный текущий → `422 invalid_current_password`; слабый новый → `422
  validation_error`. Остальные сессии отзываются.
- `POST /api/profile/history/delete` `{confirm: true}` → 200
  `{deleted_chats: N}`; без `confirm` → `422 validation_error`. Удаляются чаты
  пользователя (каскадно сообщения и оценки), аккаунт остаётся.

### FR-9. Ошибки: коды и HTTP-статусы

| Код | HTTP | Когда |
|---|---|---|
| `unauthorized` | 401 | нет/истекла сессия |
| `forbidden` | 403 | доступ запрещён политикой |
| `csrf_invalid` | 403 | CSRF-токен отсутствует/не совпал |
| `not_found` | 404 | объект не существует **или** чужой |
| `validation_error` | 422 | поля, лимиты, курсор, невалидный ввод |
| `invalid_current_password` | 422 | неверный текущий пароль |
| `generation_in_progress` | 409 | в чате уже идёт стрим (вторая вкладка) |
| `chat_limit_reached` | 409 | 40 сообщений в чате |
| `llm_busy` | 503 | очередь LLM переполнена / ожидание истекло |
| `llm_unavailable` | 503 | LLM недоступна, таймаут, сеть |
| `internal_error` | 500 | прочее; `details.request_id` обязателен |

Кода `rate_limited` **нет**: пользовательская квота отменена (CC-23, D-08).

### FR-10. `/health`

- `GET /health/live` → `200 text/plain "ok"`, без обращений к зависимостям.
  Используется healthcheck’ом compose.
- `GET /health` → 200 (или 503 при `db=down`):

```json
{"status": "ok|degraded|down",
 "version": "0.1.0",
 "checked_at": "2026-11-25T10:00:00Z",
 "components": {
   "db": "ok|down",
   "qdrant": "ok|down",
   "llm": "ok|degraded|unknown",
   "kb": {"status": "ok|empty|not_indexed", "documents": 0,
          "chunks": 0, "indexed_at": null}}}
```

- `503` только при `db=down` (сервис недееспособен). Пустая БЗ → `degraded` + 200
  (CC-01, продукт отвечает в режиме `ungrounded`).
- `kb.status = empty` при 0 документов; `not_indexed` при документах без чанков
  в Qdrant; `ok` иначе.
- LLM **не опрашивается** на каждый запрос: результат `GET {LLM_BASE_URL}/models`
  с таймаутом 3 с кэшируется на 60 с; до первого опроса — `unknown`; 404/405 от
  провайдера → `unknown` (не `down`).
- Эндпоинты публичные, без секретов и внутренних URL; в логи не пишутся (CC-20).

## 5. Данные и точки интеграции

### 5.1. DDL (PostgreSQL 16, Alembic)

Enum’ы — `text` + `CHECK` (проще миграции, чем нативные типы). Все времена —
`timestamptz`, UTC.

```sql
CREATE TABLE users (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email           varchar(254) NOT NULL UNIQUE,       -- хранится в lower-case
    password_hash   varchar(255) NOT NULL,              -- Argon2id
    consent_at      timestamptz  NOT NULL,
    consent_version varchar(32)  NOT NULL,
    created_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  bytea       NOT NULL UNIQUE,            -- sha256(raw token)
    csrf_token  varchar(64) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL                    -- created_at + 7 дней
);
CREATE INDEX ix_sessions_user    ON sessions(user_id);
CREATE INDEX ix_sessions_expires ON sessions(expires_at);

CREATE TABLE chats (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        bigint       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title          varchar(120) NOT NULL,
    messages_count int          NOT NULL DEFAULT 0,
    created_at     timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX ix_chats_user_activity ON chats(user_id, updated_at DESC, id DESC);

CREATE TABLE messages (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id    bigint      NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    seq        int         NOT NULL,
    role       text        NOT NULL CHECK (role IN ('user','assistant')),
    content_md text,
    status     text        CHECK (status IN
                 ('grounded','ungrounded','no_answer','no_reply','stopped')),
    sources    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    meta       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_messages_seq UNIQUE (chat_id, seq),
    CONSTRAINT ck_messages_user_content
        CHECK (role <> 'user'   OR (content_md IS NOT NULL AND status IS NULL)),
    CONSTRAINT ck_messages_assistant_status
        CHECK (role <> 'assistant' OR status IS NOT NULL)
);
CREATE INDEX ix_messages_chat_seq ON messages(chat_id, seq DESC);

CREATE TABLE message_ratings (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id bigint      NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    user_id    bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    value      text        NOT NULL CHECK (value IN ('like','dislike')),
    comment    varchar(1000),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ratings_user ON message_ratings(user_id);
```

Правила:

- `messages.seq` выдаётся как `messages_count + 1` в той же транзакции, что и
  `INSERT` сообщения и `UPDATE chats SET messages_count = messages_count + 1,
  updated_at = now()`; `UNIQUE(chat_id, seq)` ловит гонки.
- Удаление чата/пользователя каскадно чистит сообщения и оценки.
- `messages.sources` — **снимок** метаданных цитат (не FK на индекс): история не
  меняется при переиндексации.
- `messages.meta` — служебное (модель, длительность, retrieval score); наружу не
  отдаётся.
- Таблица документов корпуса (`kb_documents`) создаётся в 04; 03 требует от неё
  только counts для `/health` (OQ-03-01).

### 5.2. Кэш и очередь

- Очередь LLM — in-process: `asyncio.Semaphore(LLM_MAX_CONCURRENCY)` + счётчик
  ожидающих (`LLM_QUEUE_MAX`). При рестарте сбрасывается (активных стримов нет).
- Активные стримы — in-process `dict[chat_id → task]`; при рестарте история
  остаётся, незавершённые assistant-сообщения не появляются (они пишутся только
  по завершении).
- `last_seen_at` обновляется не чаще раза в 5 минут (иначе запись на каждый
  запрос).

### 5.3. Cookie

| Cookie | Значения | Флаги |
|---|---|---|
| `sa_session` | сырой токен 32 байта (base64url) | `HttpOnly`, `SameSite=Lax`, `Secure` при `COOKIE_SECURE=true`, `Path=/`, `Max-Age=7d` |
| `csrf_token` | дублирует `sessions.csrf_token` | `SameSite=Lax`, `Secure` аналогично, `Path=/`, **не** `HttpOnly` |

CSRF дополнительно отдаётся в HTML (`<meta name="csrf-token">` и hidden-поле).

### 5.4. Переменные окружения (`Settings`)

| Переменная | Default | Назначение |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL (asyncpg) |
| `QDRANT_URL` | — | Qdrant |
| `QDRANT_COLLECTION` | `kb_chunks` | имя коллекции (04) |
| `LLM_PROVIDER` | `local` | **только диагностика** (CC-09) |
| `LLM_BASE_URL` | — | база OpenAI-совместимого API (`…/v1`) |
| `LLM_API_KEY` | — | ключ |
| `LLM_MODEL` | — | модель генерации |
| `LLM_MAX_CONCURRENCY` | `4` | слоты |
| `LLM_QUEUE_MAX` | `8` | размер буфера ожидания |
| `LLM_QUEUE_TIMEOUT_SECONDS` | `60` | ожидание слота |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `90` | таймаут генерации |
| `EMBEDDING_PROVIDER` | `local` | только диагностика |
| `EMBEDDING_BASE_URL` | — | база `…/v1`, **без** `/embeddings` |
| `EMBEDDING_API_KEY` | — | ключ (новое поле) |
| `EMBEDDING_MODEL` | — | модель эмбеддингов |
| `EMBEDDING_TIMEOUT_SECONDS` | `60` | таймаут |
| `RETRIEVAL_TOP_K` | `10` | top-k выдачи (05) |
| `RETRIEVAL_SCORE_THRESHOLD` | — | порог, значение в 05 |
| `KB_DATA_DIR` | `/data/kb` | корпус |
| `SESSION_TTL_DAYS` | `7` | TTL сессии |
| `CHAT_MAX_MESSAGES` | `40` | потолок чата |
| `COOKIE_SECURE` | `true` | флаг `Secure` у cookie |
| `DEBUG` | `false` | `/docs`, подробности ошибок |
| `APP_VERSION` | `0.1.0` | для `/health` |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | — | bootstrap-админ (07) |

Удаляется: `RATE_LIMIT_PER_HOUR` (CC-23). `LLM_PROVIDER` не влияет на логику —
только стартовый лог и `/health`.

## 6. Граничные случаи

| Ситуация | Поведение |
|---|---|
| Пустой/пробельный вопрос | `422 validation_error`, ничего не создаётся |
| `chat_id` чужого чата | `404 not_found` (не 403) |
| Вторая вкладка шлёт вопрос во время стрима | `409 generation_in_progress` |
| 40-е сообщение в чате | `409 chat_limit_reached`, UI предлагает новый чат |
| Очередь LLM переполнена | `503 llm_busy`, текст остаётся в поле |
| Ожидание в очереди > 60 с | `error {llm_busy}`, user-сообщение остаётся, статус `no_reply` |
| LLM недоступна/таймаут | `error {llm_unavailable}` или HTTP 503 до стрима |
| Обрыв SSE клиентом | генерация отменена, `no_reply`, текст не сохраняется |
| «Остановить» до первого delta | пустое assistant-сообщение `stopped` |
| «Остановить» после завершения | `204`, состояние не меняется |
| Модель вернула 0 валидных цитат | `ungrounded`, `sources: []` (05) |
| Модель вернула пустой ответ | `no_answer` (не ошибка) |
| Пустая БЗ | `ungrounded` + баннер, `/health` → `degraded` |
| Истёкшая сессия на API | `401 unauthorized`, UI → `/auth?next=…` |
| Истёкшая сессия на странице | `303 /auth?next=…` |
| Невалидный курсор пагинации | `422 validation_error` |
| Смена пароля при активных сессиях | остальные отозваны, текущая жива |
| Удаление истории при открытом чате | `200`, UI → `/` |
| Двойной клик по «Оценить» | `PUT` идемпотентен, дублей нет |
| Оценка чужого сообщения | `404 not_found` |
| Повторная регистрация email | ошибка под полем, 200-фрагмент |
| CSRF-токен отсутствует | `403 csrf_invalid` (для форм — фрагмент + 403) |

## 7. Нефункциональные требования

- **Производительность:** первый `delta` — без ожидания полного ответа; стартовая
  выдача истории — 2 сообщения; список чатов — keyset-пагинация без `OFFSET`.
- **Устойчивость:** ошибки зависимостей не превращаются в assistant-сообщения;
  `no_reply` — единственный «пустой» статус в истории.
- **Безопасность:** Argon2id, сессии в БД (в куке — только токен), CSRF, `Secure`
  + `HttpOnly`, `SameSite=Lax`, 404 вместо 403 для чужих объектов, экранирование
  пользовательского текста и санитайзинг Markdown (01).
- **Приватность:** в логах нет текста вопросов/ответов, email логируется только
  как `user_id`; `/health` не содержит ПДн и внутренних адресов.
- **Совместимость:** клиент игнорирует неизвестные события/поля; новые
  необязательные поля не ломают UI.
- **Наблюдаемость:** `structlog` JSON (`request_id, user_id, chat_id, route,
  status, duration_ms, error_code`); 4xx — `warning`, 5xx — `error`; `/health` и
  `/health/live` не логируются.
- **Локализация:** сообщения об ошибках — на русском, коды — латиницей.

## 8. Критерии приёмки

- [ ] Чат создаётся первым непустым сообщением; `POST /api/chats` отсутствует.
- [ ] SSE: `meta` первым, `done`/`error` терминальны и несовместимы, ping каждые
      15 с, неизвестные события игнорируются.
- [ ] `sources` содержит только цитированные чанки, `source_no` 1…n, `snippet`
      ≤ 400, UI группирует по файлу.
- [ ] Остановка сохраняет частичный текст (`stopped`), обрыв — `no_reply` без
      текста; повторный stop → 204.
- [ ] 4 слота LLM + буфер 8; переполнение → мгновенный `503 llm_busy`; ожидание
      > 60 с → `error llm_busy`.
- [ ] Лимит 40 сообщений → `409 chat_limit_reached` и плашка в UI.
- [ ] История: старт с 2 сообщений, догрузка по 20, `has_more`/`oldest_seq`.
- [ ] Оценка: upsert, изменение, удаление (`value:null`), восстановление после
      перезагрузки, чужие → 404.
- [ ] Ошибки соответствуют таблице кодов; `rate_limited` отсутствует.
- [ ] `/health/live` не зависит от БД; `/health` даёт `degraded` при пустой БЗ и
      503 только при `db=down`; LLM-статус кэшируется.
- [ ] Cookie: `sa_session` HttpOnly, TTL 7 дней, смена пароля отзывает прочие
      сессии; CSRF-токен привязан к сессии.
- [ ] В логах нет текста сообщений; `request_id` есть в каждом ответе об ошибке.
- [ ] `Settings` содержит все переменные из §5.4; `.env.example` исправлен
      (`EMBEDDING_BASE_URL` без `/embeddings`, добавлен `EMBEDDING_API_KEY`).

## 9. Журнал решений

| ID | Решение |
|---|---|
| CC-01 | Пустая/непроиндексированная БЗ не блокирует отправку: `ungrounded` + баннер и `kb.status`. Закрывает OQ-01/D-04. |
| CC-02 | SSE: `meta → delta* → sources → done`; `error` терминален; HTTP-ошибки до старта стрима; heartbeat 15 с; частичный ответ при обрыве не сохраняется. |
| CC-03 | Ограниченная очередь LLM: 4 слота, буфер 8, ожидание ≤ 60 с; переполнение → мгновенный `llm_busy`. Закрывает OQ-04. |
| CC-04 | Серверные сессии в PostgreSQL, в cookie — сырой токен, в БД — SHA-256; TTL 7 дней абсолютный. CSRF — см. CC-30. |
| CC-05 | `BIGINT IDENTITY`; `timestamptz` UTC; `seq` в рамках чата; источники — JSONB-снимок; оценки — отдельная таблица `UNIQUE(message_id)`. |
| CC-06 | `consent_at` + `consent_version` в `users`. Закрывает OQ-05. |
| CC-07 | `/api` без версии; единый конверт ошибок; 401 JSON для API; `/docs` выключен в prod. |
| CC-08 | `/health/live` + `/health`; 503 только при `db=down`; LLM-статус кэшируется 60 с. |
| CC-09 | `LLM_PROVIDER`/`EMBEDDING_PROVIDER` — только диагностика; нормализация env. Закрывает PR-15. |
| CC-10 | Источник истины — `docs/research/`; README синхронизируется отдельной задачей. |
| CC-11 | ~~Один пустой чат на пользователя, переиспользование через `POST /api/chats`~~ — **отменено CC-27**. |
| CC-12 | UI блокирует ввод во время стрима, показывает «Остановить»; серверный 409 остаётся защитой от второй вкладки. |
| CC-13 | `PUT /api/messages/{id}/rating` — upsert; `value:null` удаляет; комментарий без оценки невозможен; чужие → 404. |
| CC-14 | `top_k = 10`; в `sources` — только цитированные чанки (≤ 8) с метаданными файла. |
| CC-15 | Лимиты: email ≤ 254, пароль 8…128, вопрос ≤ 2000, комментарий ≤ 1000, заголовок 60, `limit` 1…50 (15). |
| CC-16 | Пользовательский rate-limit 100/ч отменён как излишний. |
| CC-17 | Auth — HTML-формы (303/`HX-Redirect`); `/api/*` — JSON. |
| CC-18 | `status ∈ grounded\|ungrounded\|no_answer\|no_reply`; ошибки не персистятся; `meta` JSONB для отладки. Расширено CC-21 (`stopped`). |
| CC-19 | История — ленивая загрузка; потолок чата 40 сообщений. |
| CC-20 | `structlog`, `request_id`, без текста сообщений в логах, health не логируется. |
| CC-21 | «Остановить»: `POST /api/chats/{id}/stop`, частичный текст сохраняется (`stopped`); обрыв без stop → `no_reply`. |
| CC-22 | `RETRIEVAL_TOP_K=10` в `Settings` и 05; JSON — чанки, UI — группировка по файлу. |
| CC-23 | `rate_limited` и `RATE_LIMIT_PER_HOUR` удалены; защита от перебора паролей — 07 (дельта D-08 к 02). |
| CC-24 | История: старт с 2 последних сообщений, догрузка по 20 до потолка 40. |
| CC-25 | 40 сообщений → ввод блокируется, плашка с переходом в новый чат; `CHAT_MAX_MESSAGES=40`. |
| CC-26 | Чат не создаётся, пока не задан первый вопрос. |
| CC-27 | `POST /api/chats` удалён; единая точка `POST /api/messages {chat_id?, text}`; `meta` отдаёт `chat_id`/`chat_title`. Отменяет CC-11. |
| CC-28 | JSON источников — чанки; UI группирует по файлу; `[n]` ведёт к чанку. |
| CC-29 | Остановка завершается `done {status:"stopped"}`, `stop` идемпотентен (204), пустой остановленный ответ сохраняется. |
| CC-30 | CSRF-токен хранится в сессии и сравнивается с присланным значением (а не только с cookie) — строже классического double-submit. |

### Дефекты и дельты, зафиксированные здесь

| ID | Что | Куда |
|---|---|---|
| D-08 | Отмена лимита 100 запросов/час (FR-8, edge case «429», критерий приёмки 02) | 02 (отменяет) |
| D-09 | `POST /api/chats` удалён, введён `POST /api/messages` | 01 §9 |
| D-10 | Остановка генерации переходит из бэклога в скоуп (UI-25) | 01 §16 |
| D-11 | Пустой чат как сущность исчезает; UI-04 «Новый чат» = локальный пустой экран | 01 §5 |
| D-12 | Источники: чанки в JSON, файлы в UI (уточнение §6 01) | 01 |
| D-13 | Ленивая загрузка истории (UI-26) | 01 |
| D-14 | Потолок 40 сообщений на чат | 02, 06 |
| F-01 | `EMBEDDING_BASE_URL` содержит `/embeddings` (двойной путь) | `.env.example`, `config.py` |
| F-02 | Нет `EMBEDDING_API_KEY`, `LLM_MAX_CONCURRENCY`, таймаутов, `RETRIEVAL_TOP_K`, `CHAT_MAX_MESSAGES` | `config.py` |
| F-03 | `deploy/Caddyfile` отсутствует | 09 |
| F-04 | README противоречит 01/02 | 10 / отдельная задача |
| F-05 | `qdrant-client[fastembed]` при внешних эмбеддингах | `pyproject.toml` |

## 10. Открытые вопросы

- **OQ-03-01** (→ 04): имя и структура таблицы документов корпуса (`kb_documents`),
  формат `chunk_id`, источник counts для `/health`.
- **OQ-03-02** (→ 05): порог релевантности (`RETRIEVAL_SCORE_THRESHOLD`) и способ
  валидации цитат — определяют долю `grounded`/`ungrounded`/`no_answer`.
- **OQ-03-03** (→ 07): защита `/auth/login` от перебора (задержка/счётчик по
  email+IP), bootstrap-админ.
- **OQ-03-04** (→ 09): HTTPS, домен, бэкапы, ротация логов; healthcheck `app` на
  `/health/live`.
- **OQ-03-05** (→ 06): поведение списка при массовом удалении, сортировка после
  удаления истории; форма ответа при `deleted_chats = 0`.
- **OQ-03-06**: значения `LLM_QUEUE_MAX=8` и таймаутов приняты как рабочие;
  могут уточняться нагрузочным smoke в M2 без смены контракта.

## 11. Бэклог

- Версионирование API (`/api/v1`) при появлении внешних клиентов.
- Пагинация сообщений сверх 40 и увеличение потолка чата.
- Идемпотентные ключи для POST (защита от дублей при ретраях сети).
- Админский флаг и UI (M5), переиндексация из интерфейса.
- Обезличивание истории, удаление аккаунта (02).
- WebSocket вместо SSE (не требуется при текущей нагрузке).
