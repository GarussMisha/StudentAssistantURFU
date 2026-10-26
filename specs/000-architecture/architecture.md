# Архитектура системы

> Фича: 000-architecture
> Спецификация: ./spec.md · План: ./plan.md

Схемы в Mermaid — Obsidian рендерит их в режиме чтения.

## 1. Контекст (C4, уровень 1)

```mermaid
flowchart LR
    subgraph context
        student(["Студент"])
        teacher(["Преподаватель /<br/>тестировщик"])
        admin(["Администратор БЗ"])
        sys["StudentAssistant<br/>ИИ-ассистент по проектной деятельности"]
        llm["LLM API<br/>LM Studio / OpenRouter"]
        emb["Embedding API<br/>/v1/embeddings"]

        student -->|"задаёт вопросы,<br/>оценивает ответы"| sys
        teacher -->|"проходит сценарии,<br/>смотрит отчёт качества"| sys
        admin -->|"управляет БЗ<br/>и пользователями"| sys
        sys -->|"chat completions"| llm
        sys -->|"векторизация"| emb
    end
```

Внешние зависимости: **LLM API**, **Embedding API** (в dev — LM Studio, в prod —
внешние провайдеры) и **корпус БЗ** (файлы, которые загружает администратор).
Сервис не держит модели в памяти: на VPS только приложение, PostgreSQL и Qdrant.

## 2. Контейнеры (C4, уровень 2)

```mermaid
flowchart TB
    browser["Браузер<br/>Jinja2 + HTMX"]

    subgraph vps["VPS · Docker Compose"]
        caddy["Caddy<br/>TLS + reverse proxy"]

        subgraph app["FastAPI · один процесс"]
            web["Web / API<br/>роутеры, шаблоны"]
            auth["Auth<br/>argon2 + серверные сессии"]
            chat["Chat<br/>SSE-стриминг"]
            admin["Admin<br/>документы, пользователи"]
            ingest["Ingestion<br/>конвертация, чанкинг"]
            rag["RAG Orchestrator<br/>rewrite → retrieve → prompt"]
            gw["LLM Gateway<br/>провайдер, fallback, ретраи"]
            embc["Embedding Client"]
        end

        pg[("PostgreSQL 16<br/>users, conversations,<br/>messages, documents, chunks")]
        qd[("Qdrant<br/>dense + sparse<br/>коллекции kb_vN")]
        files[("Volume /data/kb<br/>raw + markdown")]
    end

    llm["LLM API<br/>LM Studio / OpenRouter"]
    embs["Embedding API<br/>LM Studio / внешний"]

    browser -->|HTTPS| caddy --> web
    web --> auth
    web --> chat --> rag
    web --> admin --> ingest
    ingest --> embc
    rag --> embc
    embc -->|"/v1/embeddings"| embs
    rag --> qd
    rag --> pg
    rag --> gw
    gw -->|"/v1/chat/completions"| llm
    ingest --> pg
    ingest --> qd
    ingest --> files
    auth --> pg
    chat --> pg
```

**Почему один процесс:** микросервисы здесь не окупаются — команда из двух
разработчиков, нагрузка бытовая. Модели вынесены за API, поэтому приложение
остаётся лёгким и может жить на дешёвом VPS. Если индексация станет тяжёлой,
её вынесение в отдельный воркер не потребует менять контракты.

## 3. Основной сценарий: вопрос → ответ

```mermaid
sequenceDiagram
    autonumber
    actor U as Студент
    participant B as Браузер
    participant API as FastAPI
    participant RAG as RAG Orchestrator
    participant PG as PostgreSQL
    participant EMB as Embedding API
    participant QD as Qdrant
    participant LLM as LLM API

    U->>B: вопрос в диалоге
    B->>API: POST /api/conversations/{id}/messages
    API->>PG: сохранить вопрос + загрузить историю в окне токенов
    API-->>B: SSE: stream открыт

    API->>RAG: answer(question, history)
    alt запрос — продолжение диалога
        RAG->>LLM: переформулировать в самостоятельный вопрос
        LLM-->>RAG: standalone query
    end

    RAG->>EMB: embed(query) + инструкция поиска
    EMB-->>RAG: вектор 1024d
    RAG->>QD: hybrid search (dense + sparse, RRF, top_k)
    QD-->>RAG: чанки, метаданные, scores

    alt ни один чанк не прошёл порог
        RAG-->>API: шаблонный отказ + темы БЗ
        API-->>B: SSE: token (отказ)
    else контекст найден
        RAG->>LLM: stream(system + history + context + question)
        loop поток генерации
            LLM-->>RAG: токен
            RAG-->>API: токен
            API-->>B: SSE: token
        end
    end

    API->>PG: сохранить ответ, источники, метрики
    API-->>B: SSE: sources + done
```

**Ключевое решение:** сначала сохраняем вопрос, потом генерируем. Если LLM упал или
клиент отвалился — вопрос и частичный ответ не теряются.

**Окно контекста** задаётся бюджетом токенов модели, а не фиксированным числом
сообщений: история подбирается с конца, пока помещается в лимит. Старые сообщения
остаются в БД и видны в UI, но в промпт не попадают.

## 4. Аутентификация и администрирование

```mermaid
sequenceDiagram
    autonumber
    actor U as Пользователь
    participant API as FastAPI
    participant PG as PostgreSQL

    Note over API,PG: Старт приложения
    API->>PG: admin существует?
    alt нет
        API->>PG: INSERT users из ADMIN_EMAIL / ADMIN_PASSWORD
    end

    U->>API: POST /auth/register (email, password, repeat)
    API->>API: валидация + Argon2id
    API->>PG: INSERT users (email UNIQUE)
    alt email занят
        PG-->>API: unique violation
        API-->>U: 409 "email уже зарегистрирован"
    else успех
        API->>PG: INSERT auth_sessions (token_hash, expires_at)
        API-->>U: Set-Cookie sa_session (HttpOnly, Secure, SameSite=Lax)
    end

    U->>API: POST /auth/login
    API->>PG: SELECT user + verify Argon2id
    API->>PG: INSERT auth_sessions
    API-->>U: Set-Cookie + редирект на чат
```

```mermaid
sequenceDiagram
    autonumber
    actor A as Администратор
    actor U as Пользователь
    participant API as FastAPI
    participant PG as PostgreSQL

    U->>A: просит сбросить пароль (вне системы)
    A->>API: POST /api/admin/users/{id}/reset-password
    API->>API: сгенерировать пароль
    API->>PG: UPDATE password_hash + revoke sessions
    API-->>A: новый пароль (показан один раз)
    A->>U: передаёт пароль
```

Сессии серверные: в cookie — токен, в БД — его хеш. Сброс пароля отзывает все
сессии пользователя. Пароль никогда не пишется в логи.

## 5. Пайплайн базы знаний

```mermaid
flowchart LR
    A["Загрузка через админку<br/>PDF, DOCX, MD, HTML"] --> B{"формат и размер<br/>допустимы?"}
    B -->|нет| E1["failed:<br/>неподдерживаемый формат<br/>или > 50 МБ"]
    B -->|да| C["сохранить оригинал<br/>+ checksum"]
    C --> D["конвертация в Markdown<br/>MarkItDown / pymupdf4llm"]
    D --> E{"текст извлечён?"}
    E -->|нет| E2["failed:<br/>скан без текстового слоя"]
    E -->|да| F["заголовки и метаданные<br/>раздел, страница, автор"]
    F --> G["чанкинг по заголовкам MD<br/>600–900 токенов,<br/>overlap 15%"]
    G --> H["эмбеддинги<br/>/v1/embeddings"]
    H --> I["запись в Qdrant<br/>коллекция kb_vN"]
    I --> J["запись chunks в PostgreSQL"]
    J --> K["переключение алиаса<br/>kb_current → kb_vN"]
    K --> L["статус документа ready"]
```

**Markdown как канонический формат.** Конвертация происходит один раз при загрузке.
Дальше и чанкинг, и цитаты, и просмотр фрагмента работают с одним представлением —
не нужно поддерживать отдельные ветки для PDF, DOCX и HTML.

**Blue/green индексация:** новая версия собирается в отдельной коллекции, поиск до
последнего момента идёт по старой. Переключение алиаса атомарно, откат — вернуть
алиас на предыдущую коллекцию.

**Хранение файлов:** `/data/kb/raw/{document_id}.{ext}` — оригинал,
`/data/kb/md/{document_id}.md` — результат конвертации. В git не попадает ничего,
кроме манифеста и кода.

## 6. Состояния документа

```mermaid
stateDiagram-v2
    [*] --> pending: файл загружен
    pending --> processing: индексация запущена
    processing --> ready: чанки записаны, алиас переключён
    processing --> failed: ошибка конвертации, эмбеддинга или записи
    failed --> pending: повторный запуск
    ready --> pending: загружена новая версия (другой checksum)
```

## 7. Развёртывание

**Разработка (localhost):**

```mermaid
flowchart LR
    browser["Браузер"] --> app["app:8000<br/>uvicorn --reload"]
    app --> pg[("PostgreSQL<br/>localhost:5432")]
    app --> qd[("Qdrant<br/>localhost:6333")]
    app --> files[("локальная папка<br/>data/kb")]
    app -->|"127.0.0.1:1234"| lm["LM Studio<br/>chat + embeddings"]
```

**Продакшен (VPS):**

```mermaid
flowchart LR
    browser["Браузер"] -->|"443 HTTPS"| caddy["Caddy<br/>авто-TLS"]
    caddy --> app["app:8000<br/>FastAPI + Uvicorn"]
    app --> pg[("postgres:5432<br/>volume pg_data")]
    app --> qd[("qdrant:6333<br/>volume qdrant_data")]
    app --> files[("volume kb_data")]
    app -->|"HTTPS"| ext["LLM API<br/>Embedding API"]
    cron["cron: бэкап, healthcheck"] --> app
    app --> logs["docker logs<br/>JSON-строки"]
```

На VPS живут только приложение и БД. Модели — за API, поэтому серверу не нужен GPU
и большой объём RAM.

## 8. Режимы LLM и эмбеддингов

```mermaid
flowchart LR
    cfg[".env"] --> gw["LLMGateway"]
    cfg --> embc["Embedding Client"]
    gw -->|"LLM_PROVIDER=local"| lm["LM Studio<br/>http://127.0.0.1:1234/v1"]
    gw -->|"LLM_PROVIDER=openrouter"| or["OpenRouter<br/>https://openrouter.ai/api/v1"]
    embc -->|"EMBEDDING_BASE_URL"| embs["Embedding API<br/>LM Studio / внешний"]
```

| Режим | Когда используем | Плюсы | Ограничения |
|-------|------------------|-------|-------------|
| local (LM Studio) | разработка, eval, демо без интернета | 0 ₽, нет лимитов, данные не уходят | нужен мощный ПК, качество зависит от модели |
| remote (OpenRouter) | прод на VPS, приёмка | не грузит сервер, сильные модели | лимиты free-моделей, нужен интернет |

Контракт в обоих случаях одинаковый — OpenAI-совместимые `/v1/chat/completions` и
`/v1/embeddings`. Меняются только `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
`EMBEDDING_BASE_URL`; код и пайплайн те же.

**Подводный камень с адресом.** `127.0.0.1` внутри контейнера — это сам контейнер, а
не хост. Если приложение запущено в Docker:

- Docker Desktop (Windows/macOS) — `http://host.docker.internal:1234/v1`;
- Linux, bridge-сеть — `http://172.17.0.1:1234/v1` (либо `network_mode: host`);
- запуск без Docker — `http://127.0.0.1:1234/v1`.

Это фиксируется в `.env.example` и в README, иначе первый запуск local-режима
упирается в «connection refused».

**Открытый вопрос:** OpenRouter не предоставляет embeddings. Для продакшена нужен
отдельный провайдер эмбеддингов — LM Studio на отдельном хосте, Jina/Cohere или
GigaChat embeddings. Интерфейс уже универсальный, решение не блокирует разработку.

## 9. Карта компонентов и спецификаций

```mermaid
flowchart TB
    subgraph done["Сделано"]
        c0["000-architecture<br/>система целиком"]
    end
    subgraph next["Следующие спеки"]
        c1["001-knowledge-base<br/>корпус, конвертация, чанкинг"]
        c2["002-rag-core<br/>retrieval, промпт, цитаты"]
        c3["003-backend-api<br/>auth, диалоги, админка, фидбек"]
        c4["004-web-ui<br/>чат, источники, история"]
        c5["005-evaluation<br/>golden set, метрики, отчёт"]
        c6["006-deployment<br/>compose, VPS, CI, мониторинг"]
    end
    c1 --> c2 --> c3 --> c4
    c2 --> c5
    c3 --> c6
```

Порядок неслучаен: **качество RAG проверяется до того, как появится красивый UI.**
Если retrieval плохой, никакой интерфейс это не спасёт.
