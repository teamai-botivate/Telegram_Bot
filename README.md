# botivate-bot

Multi-tenant AI-powered data assistant backend. Business users ask questions in plain English over Telegram or WhatsApp and receive accurate, formatted answers drawn from their own company database (Postgres or Google Sheets).

## How It Works

Incoming messages are normalised into a platform-neutral `BotMessage` and passed through a single pipeline:

```
Webhook (Telegram / WhatsApp)
  → Intent Classification (hardcoded rules → learned rules → default data_query)
  → Tenant Lookup + Credential Routing
  → SQL Generation (OpenAI gpt-4.1) + EXPLAIN Validation + Self-Healing Retry
  → Smart Response Formatting (template for simple results, fast LLM for complex)
  → Platform Reply (Telegram / WhatsApp)
```

Google Sheets tenants skip SQL entirely — live rows are fetched and answered directly by the LLM.

## Local Setup

1. Clone the repository and create your environment file:

```bash
cp .env.example .env
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the app:

```bash
uvicorn app.main:app --reload
```

4. Apply migrations and seed demo data (optional):

```bash
alembic upgrade head
python seed_data.py
python scripts/seed_examples_minutes_of_meeting.py
```

## Environment Variables

**Required:**

| Variable | Description |
|---|---|
| `DATABASE_URL` | NeonDB Postgres URL (Botivate meta DB) |
| `FERNET_SECRET_KEY` | Symmetric encryption key for tenant credentials |
| `ADMIN_SECRET_TOKEN` | Protects all `/admin/*` endpoints |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `OPENAI_API_KEY` | Used for SQL generation and embeddings |

**LLM Models:**

| Variable | Default | Description |
|---|---|---|
| `SQL_GENERATION_MODEL` | `gpt-4.1` | Main model — SQL generation only (always OpenAI) |
| `FAST_LLM_PROVIDER` | `openai` | Provider for fast tasks: `openai`, `groq`, or `cerebras` |
| `FAST_LLM_API_KEY` | — | API key for Groq or Cerebras |
| `FAST_LLM_MODEL` | provider default | Model name for the fast provider |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model |

Groq and Cerebras expose OpenAI-compatible APIs. Setting `FAST_LLM_PROVIDER=groq` with a Groq key routes intent classification, DB routing, and response formatting to Llama 3.3 70B at near-zero cost.

**Onboarding:**

| Variable | Description |
|---|---|
| `ONBOARDING_JWT_SECRET` | Signs single-use onboarding tokens |
| `ONBOARDING_BASE_URL` | Base URL of the self-service setup form |

**Main DB Sync (optional):**

| Variable | Default | Description |
|---|---|---|
| `BOTIVATE_MAIN_DB_URL` | — | Supabase URL to sync registered clients from |
| `BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES` | `15` | How often to sync |

**Tuning (all optional):**

| Variable | Default | Description |
|---|---|---|
| `SQL_DEFAULT_ROW_LIMIT` | `50` | Default LIMIT on SQL queries |
| `SQL_FULL_ROW_LIMIT` | `500` | Max rows for full-result queries |
| `RUNTIME_SCHEMA_CACHE_TTL_SECONDS` | `300` | How long to cache tenant schema introspection |
| `SHEETS_CACHE_TTL_SECONDS` | `60` | How long to cache Google Sheets data |
| `ENABLE_QUERY_LEARNING` | `true` | Store successful (question, SQL) pairs for few-shot retrieval |
| `TENANT_DB_CONNECT_TIMEOUT_SECONDS` | `30` | Timeout for tenant DB connections |

**WhatsApp (optional):**
`WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WEBHOOK_VERIFY_TOKEN`

## Webhook Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook/telegram` | Telegram messages and inline button callbacks |
| `POST` | `/webhook/whatsapp` | WhatsApp inbound messages |
| `GET` | `/webhook/whatsapp` | WhatsApp webhook verification handshake |
| `GET` | `/health` | Health check (used by Render) |

Both POST webhooks always return HTTP 200 immediately. Processing runs asynchronously in the background.

## Admin Endpoints

All routes protected by `x-admin-token: <ADMIN_SECRET_TOKEN>`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/tenant/connect-db` | Connect a DB credential to a tenant |
| `POST` | `/admin/tenant/create-full` | Create a full tenant record |
| `POST` | `/admin/tenant/{id}/generate-link` | Issue a Telegram magic-link JWT |
| `POST` | `/admin/tenant/{id}/refresh-schema` | Re-run schema introspection, update blueprint + hints |
| `GET` | `/admin/tenant/{id}/test-query` | Run a raw SQL query for debugging |
| `GET/POST/DELETE` | `/admin/tenant/{id}/examples` | Manage few-shot query examples |
| `POST` | `/admin/sync/trigger` | Manually trigger Main DB sync |

Onboarding form endpoints (`/api/onboard/*`) use JWT auth, not the admin token.

## Self-Service Onboarding

When a registered client messages the bot before completing setup:

1. The bot detects the unboarded state and generates a single-use JWT (30-min expiry).
2. The user receives a setup link pointing to `ONBOARDING_BASE_URL`.
3. `GET /api/onboard/context` — returns company name and purchased products for the form.
4. `POST /api/onboard/submit` — validates the DB connection, encrypts credentials, stores them, and triggers schema refresh.
5. The user returns to Telegram/WhatsApp and can immediately start querying.

## Registering Webhooks

**Telegram:**
```
https://<your-service>.onrender.com/webhook/telegram
```

**WhatsApp (Meta Developer Dashboard):**
- Callback URL: `https://<your-service>.onrender.com/webhook/whatsapp`
- Verify token: matches `WEBHOOK_VERIFY_TOKEN`
- Subscribe to the `messages` webhook field

## Render Deployment

Build command:
```bash
pip install -r requirements.txt
```

Start command:
```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set all required environment variables in the Render dashboard. The app never blocks startup on DB availability — health checks pass immediately.

## Running Tests

```bash
# All tests
python -m pytest tests/ -q --tb=short

# Single file
python -m pytest tests/test_bot_logic.py -x -q

# Single test
python -m pytest tests/test_bot_logic.py::test_name -x -q
```

`pytest.ini` sets `asyncio_mode = auto` — async tests do not need `@pytest.mark.asyncio`.

## Project Structure

```
app/
  main.py              # FastAPI app, lifespan startup/shutdown
  bot_logic.py         # Compatibility shim → app.services
  database.py          # Compatibility shim → app.db.*
  models.py            # SQLAlchemy ORM (Tenant, TenantDBCredential, TenantQueryExample, RegisteredClient, OnboardingToken)
  webhook.py           # Telegram & WhatsApp webhook handlers
  admin.py             # Admin API routes
  embeddings.py        # OpenAI embedding client + LRU cache
  db/
    core.py            # Meta engine, pool/cache dicts, exception classes
    connection.py      # Pool management, DSN resolution, SSL handling
    postgres.py        # Postgres introspection, query execution, EXPLAIN
    sheets.py          # Google Sheets adapter (gspread)
    crud.py            # SQLAlchemy ORM queries
    vector.py          # pgvector kNN retrieval
    security.py        # Fernet encryption, SQL sanitization (SELECT-only)
  services/
    core.py            # LLM config constants, user-facing message strings
    pipeline.py        # handle_message(), route_question_to_database()
    sqlgen.py          # SQL generation with few-shot retrieval
    schema.py          # Schema validation, SQL construct fixing
    intent.py          # 3-layer intent classification
    llm.py             # Multi-provider LLM client factory
    context.py         # In-memory conversation context (3 turns)
    smart_format.py    # Template for simple results, LLM for complex
    format.py          # Response formatting utilities
    runtime_memory.py  # Persistent JSON-backed learning
  auth/
    onboarding_jwt.py  # JWT issuance & verification
  platforms/
    base.py            # Platform enum, BotMessage, send_reply dispatcher
    telegram.py        # Telegram sender
    whatsapp.py        # WhatsApp stub (ready for activation)
  routers/
    onboarding.py      # GET /api/onboard/context, POST /api/onboard/submit
  sync/
    main_db_sync.py    # APScheduler: Botivate Main DB → registered_clients
  utils/
    db_tester.py       # Friendly connection error messages
alembic/               # Database migrations
tests/                 # pytest test suite
static/                # Admin portal HTML
```

## Schema Introspection

On first connect and on each `refresh-schema` call, the bot introspects the tenant's **public schema only** and generates two artefacts stored in `tenant_db_credentials`:

- `schema_blueprint` — semantic description written once (business summary, table purposes, important columns). Never auto-overwritten.
- `auto_schema_hints` — auto-generated rules from introspection: nullable status timestamps, boolean columns, enum-like text values, FK join hints. Refreshed on each schema refresh.

Both are injected into the SQL generation prompt. Introspection results are cached for 5 minutes per credential.

## Query Learning

When `ENABLE_QUERY_LEARNING=true` (default), successful `(question, SQL)` pairs are embedded and stored in `tenant_query_examples`. On each new question, the top-5 most similar past examples are retrieved via pgvector kNN and injected into the SQL prompt as few-shot examples. This improves accuracy over time without any manual work.
