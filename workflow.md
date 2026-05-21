# Workflow Documentation

This document explains how Botivate Bot works end-to-end: how a customer is onboarded, how messages flow through the pipeline, how database schema is managed via Alembic, and how the self-service form connects everything together.

---

## 1. The Big Picture

Botivate Bot is a multi-tenant Telegram and WhatsApp data assistant. Each customer (tenant) connects their own business database (Postgres or Google Sheets) once during onboarding. After that, their staff can chat with the bot in plain English and get instant answers drawn from their own data.

The system runs as a single FastAPI application. There is one Botivate-owned **meta database** (NeonDB Postgres) that stores tenant accounts, encrypted credentials, and learning data. Every customer brings their own **business database** which the bot connects to on demand using credentials decrypted at runtime.

---

## 2. How a New Tenant Is Added (End-to-End)

This is the full onboarding flow from sale to first query.

### Step 1: Sale Is Recorded in the Botivate Main Database

When a customer purchases Botivate, their company record is added to the Botivate Main Database (a Supabase Postgres instance configured via `BOTIVATE_MAIN_DB_URL`). The record contains the company name, contact person, WhatsApp number, Telegram chat ID (if known), email, and an array of purchased products with their slugs and database types.

### Step 2: Background Sync Picks Up the New Client

An APScheduler job inside the FastAPI app (`app/sync/main_db_sync.py`) runs every 15 minutes (configurable via `BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES`). It queries the Botivate Main Database, groups all the purchased product rows per client into a single `purchased_products` JSONB array, and upserts each client into the local `registered_clients` table in the meta database. From this point, the bot knows the customer exists but they are still unboarded (no `tenant_id` linked yet).

### Step 3: Customer Sends Their First Message

The customer opens the Botivate bot on Telegram or WhatsApp and sends any message. The webhook handler in `app/webhook.py` accepts the payload, wraps it into a `BotMessage` object, and dispatches it to `handle_message()` asynchronously.

Inside the pipeline, `find_registered_client_by_chat()` finds a `RegisteredClient` row matching the sender's chat ID or phone number, but sees that `tenant_id` is `NULL`. This triggers the unboarded-client branch.

### Step 4: A Single-Use Onboarding Link Is Issued

The pipeline calls `issue_token()` in `app/auth/onboarding_jwt.py`. This function:

1. Generates a new UUID (`jti`).
2. Signs a JWT with claims `sub` (registered client ID), `purpose` (`initial_setup`), `jti`, `iat`, `exp` (30-minute expiry), using `ONBOARDING_JWT_SECRET`.
3. Inserts an audit row into the `onboarding_tokens` table with `used_at = NULL` so the token can be enforced as single-use.
4. Returns the JWT string.

The bot then constructs a link of the form `https://<your-render-service>.onrender.com/static/onboard.html?token=<jwt>` (the base part comes from `ONBOARDING_BASE_URL`) and sends it to the customer as a Telegram or WhatsApp message.

### Step 5: Customer Opens the Onboarding Form

The customer taps the link and lands on `static/onboard.html`. The page is a self-contained HTML/CSS/JS app served by FastAPI's `StaticFiles` mount at `/static`. As soon as it loads, the JavaScript reads the token from the URL query string and calls `GET /api/onboard/context?token=...`.

The backend (`app/routers/onboarding.py`):

1. Decodes the JWT with `ONBOARDING_JWT_SECRET` and rejects it if expired or tampered.
2. Looks up the `onboarding_tokens` row by `jti`. Returns `409 token_already_used` if `used_at` is already set, or `401 invalid_or_expired_token` if missing.
3. Loads the matching `RegisteredClient` row and returns the company name, contact name, expiry, and the list of purchased products to the form.

The form then renders a personalised welcome message ("Welcome, *Contact Name* from *Company Name*!") and shows the appropriate fields based on the product's database type — either a single Postgres connection URL field, or a Google Sheets ID plus service account JSON field. If the customer purchased multiple products, a dropdown appears so they can pick which one they are connecting.

### Step 6: Customer Submits Credentials

When the customer hits **Submit Credentials**, the form sends a JSON POST to `/api/onboard/submit` with the token, the chosen product slug, the database type, and the credentials (either `connection_url` for Postgres or `sheet_id` + `google_credentials` for Sheets).

The submit handler performs seven actions in order:

1. **Verify JWT** — same checks as the context endpoint, plus a fresh `used_at` check.
2. **Validate body shape** — required fields depend on `db_type`. Google credentials must be valid JSON.
3. **Confirm product is purchased** — the slug from the body must match an entry in the client's `purchased_products` array; the body's `db_type` must match what was originally sold.
4. **Test the connection** — for Postgres, `test_postgres_connection()` in `app/utils/db_tester.py` opens an asyncpg connection with SSL required and a 10-second timeout. Friendly error messages are returned ("Could not resolve host", "Authentication failed", etc.).
5. **Introspect the schema** — `fetch_postgres_schema()` (or `fetch_google_sheet_data()` for Sheets) connects to the customer's database, reads all tables and columns from the `public` schema only, samples up to 5 distinct values per text column, detects nullable status columns, boolean columns, enum-like text columns, and foreign keys. It then sends this raw schema report to OpenAI which writes a semantic blueprint describing each table's business purpose. Both the semantic blueprint and the auto-generated rule-based hints are returned.
6. **Encrypt credentials** — `encrypt_credential_value()` uses Fernet symmetric encryption with `FERNET_SECRET_KEY` to wrap the connection URL and (for Sheets) the service account JSON.
7. **Single-transaction commit** — inside one SQLAlchemy transaction with `SELECT FOR UPDATE` locking on the token row, the handler creates a `Tenant` row if this is the customer's first product (or reuses the existing one for second-product setup), inserts a `TenantDBCredential` row with the encrypted credentials and schema blueprint, and marks the onboarding token as used by setting `used_at = NOW()`. The whole thing rolls back if any step fails.

The form then displays a green success state: "Credentials Received. Your credentials have been securely submitted."

### Step 7: Customer Returns to Telegram / WhatsApp

The customer goes back to the chat and asks their first real question. This time, `find_registered_client_by_chat()` sees the linked `tenant_id` and `get_tenant_by_chat_id()` returns the full Tenant record. The pipeline now runs the full SQL generation flow and returns a real answer.

### Step 8: Adding a Second Product Later

If the customer has more than one product configured but only connected one initially, they can later type `/adddb` in Telegram or use the inline "Add another database" button. The pipeline calls `issue_token()` again with `purpose=add_database` and the chosen `product_slug`. The same form is reused — `_filter_products()` in the context endpoint narrows the products array to just the one being added, so the form shows it as a read-only label rather than a dropdown.

---

## 3. The Message Pipeline (After Onboarding)

Once a tenant is fully set up, every inbound message flows through `handle_message()` in `app/services/pipeline.py`.

### Stage 1: Webhook Reception

Telegram POSTs JSON to `/webhook/telegram`; WhatsApp POSTs to `/webhook/whatsapp`. Both handlers normalise the payload into a `BotMessage(platform, chat_id, text)` and call `asyncio.create_task(handle_message(msg))` so the HTTP response is returned immediately with status 200. This is required because Telegram and WhatsApp expect fast webhook acknowledgments regardless of how long the answer takes to compute.

### Stage 2: Command and Magic-Link Detection

If the message text is `/start <token>`, it is a magic link. The token is verified and the user's chat ID is bound to a tenant. If it is `/help`, a guided message is returned. If it is `/adddb`, the add-database flow runs.

### Stage 3: Intent Classification (Three Layers)

This happens in `app/services/intent.py`. Layer 1 is a hardcoded set of greeting and jailbreak patterns ("hi", "tell me a joke", "ignore all previous instructions") — matches are instantly rejected as `off_topic`. Layer 2 checks a JSON file of rules learned from previous LLM decisions (`runtime_memory.json`). Layer 3 defaults to `data_query` — when in doubt, attempt the query. The philosophy is that a wrongly-attempted query just returns nothing, while a wrongly-blocked query frustrates a real user.

If the verdict is `off_topic`, the bot sends a polite "I can only help with your business data" message and stops.

### Stage 4: Tenant and Credential Routing

`get_tenant_by_chat_id()` looks up the tenant. If the tenant has only one database credential, it is used directly. If they have multiple (e.g. one DB per product), `route_question_to_database()` sends a compact summary of each database to the fast LLM (Groq, Cerebras, or OpenAI gpt-4.1-mini) and asks which database should answer the question. The LLM returns a JSON object with the chosen slugs.

### Stage 5: Schema Fetch and Embedding (Concurrent)

The runtime schema for the chosen credential is fetched (5-minute cache via `_runtime_schema_cache`), and the user's question is embedded with OpenAI's `text-embedding-3-small`. Both happen in parallel via `asyncio.gather()` to save latency. Only the `public` schema is ever introspected — Supabase internal schemas like `auth`, `storage`, and `vault` are explicitly excluded.

### Stage 6: Few-Shot Retrieval

`retrieve_similar_examples()` runs a pgvector kNN search over the `tenant_query_examples` table using the question's embedding. The top 5 most similar past (question, SQL) pairs are returned and injected into the SQL generation prompt as examples.

### Stage 7: SQL Generation with Chain-of-Thought

`generate_sql_query()` in `app/services/sqlgen.py` builds a single prompt containing the semantic blueprint, the live runtime schema, the auto-generated hints, the few-shot examples, the last three conversation turns, and the current question. It calls OpenAI `gpt-4.1` (the main model — always OpenAI, never Groq/Cerebras for SQL because quality matters most here). The model responds with a `<thought_process>` block describing its plan and a `<sql>` block with the final query. Only the SQL is extracted.

### Stage 8: SQL Post-Processing and EXPLAIN Validation

The raw SQL is sanitised (`_validate_generated_sql` strips fences and enforces SELECT-only), unsupported constructs are rewritten (e.g. `COUNT(DISTINCT ...) OVER()`), and count queries spanning multiple tables are expanded into `UNION ALL`. Then `EXPLAIN` is run against the tenant's database. This catches wrong column names, wrong table names, and bad joins before any data is touched. If EXPLAIN fails, the error is passed back to the LLM via `fix_sql()` which produces a corrected query; this self-heal loop runs up to 2 times.

### Stage 9: Query Execution

The validated SQL is executed against the tenant's database via a per-credential asyncpg connection pool. The pool is created on first use, cached in memory, and gracefully evicted plus retried if a stale connection is detected. Pools use `statement_cache_size=0` for PgBouncer / Supabase transaction pooler compatibility.

### Stage 10: Smart Response Formatting

`smart_format_response()` classifies the result shape: `single_count`, `single_row`, `short_list`, `medium_list`, `large_list`, or `empty`. Trivial shapes (counts, empty results, short lists) use instant Python templates with zero LLM cost. Everything else — including single-row results which often need narrative description — is sent to the fast LLM with a prompt that instructs it to produce plain-text, mobile-friendly answers without markdown, translate raw column names into plain English, and describe table structures as narratives rather than raw bullet dumps.

### Stage 11: Reply Delivery

The formatted reply is sent back via `send_reply()` which dispatches to the correct platform sender (`app/platforms/telegram.py` or `app/platforms/whatsapp.py`). Long messages are automatically chunked to fit Telegram's 4096-character limit.

### Stage 12: Learning

If query learning is enabled, the successful `(question, SQL)` pair is embedded and stored in `tenant_query_examples` in a background task. The conversation context (question, SQL, reply) is kept in memory for the last 3 turns of this chat.

---

## 4. Alembic and Schema Migrations

Alembic is the standard SQL migration tool for SQLAlchemy projects. Botivate uses it to manage the schema of the **meta database** (the Botivate-owned NeonDB instance pointed to by `DATABASE_URL`). It does **not** manage tenant business databases — those belong to the customer.

### What Alembic Does Here

The `app/models.py` file defines SQLAlchemy ORM models for `Tenant`, `TenantDBCredential`, `TenantQueryExample`, `RegisteredClient`, and `OnboardingToken`. Alembic compares these Python class definitions to the actual database schema and generates migration scripts that describe the changes needed to bring the database in sync.

Each migration is a Python file in `alembic/versions/` with an `upgrade()` function (apply the change) and a `downgrade()` function (undo it). They run in order based on a `down_revision` pointer linking each migration to its predecessor, forming a linear history.

### Current Migration History

The repository currently contains six migrations applied in this order:

1. `a9f58435a020_initial_auto_schema.py` — Creates the initial tables: `tenants`, `tenant_db_credentials` (with Fernet-encrypted `connection_url`), and `tenant_schema_map`.
2. `533983454fa0_add_google_credentials.py` — Adds the encrypted `google_credentials` column to `tenant_db_credentials` so Google Sheets tenants can store their service account JSON.
3. `b4f7e2a9c5d1_add_self_service_onboarding_tables.py` — Adds `registered_clients` and `onboarding_tokens` tables, plus the `product_slug` and `display_name` columns on `tenant_db_credentials`. This is the migration that enabled the self-service onboarding flow.
4. `c2f6d8a9b301_add_tenant_query_hints.py` — Initial version of per-tenant query hints (later replaced).
5. `df2d0f4c6d3a_add_auto_schema_hints_drop_tenant_query_hints.py` — Replaces the manual hints column with `auto_schema_hints` (auto-generated during introspection) and drops the older manual-hints table.
6. `e7a1c4b8d2f5_add_tenant_query_examples.py` — Adds the `tenant_query_examples` table with a `pgvector(1536)` column for storing embedded (question, SQL) pairs used in few-shot retrieval.

### Common Alembic Commands

```bash
# Apply all pending migrations to the database pointed to by DATABASE_URL
alembic upgrade head

# Apply only the next migration
alembic upgrade +1

# Revert the most recent migration
alembic downgrade -1

# Auto-generate a new migration from changes to models.py
alembic revision -m "describe the change" --autogenerate

# Show the current migration version applied to the database
alembic current

# Show the full migration history
alembic history
```

After editing `app/models.py` to add a column or table, the workflow is: run `alembic revision -m "description" --autogenerate`, review the generated file in `alembic/versions/` to make sure the autogenerator did the right thing (it sometimes misses things like enum types or indexes), and then run `alembic upgrade head` to apply it.

### Important Notes

The `sqlalchemy.url` in `alembic.ini` is a placeholder. The actual database URL is read from the `DATABASE_URL` environment variable at runtime by `alembic/env.py`.

Migrations should always be tested against a local copy of the database before being applied to production. The `--autogenerate` flag is a starting point, not a final answer — review the diff carefully, especially for renames (which autogenerate often misinterprets as drop + add, losing data).

On Render, migrations are not run automatically on deploy. Run `alembic upgrade head` manually using the Render shell after deploying any commit that adds a new migration.

---

## 5. How the Onboarding Form (`static/onboard.html`) Works

The form is a single self-contained HTML page styled with embedded CSS and powered by ~200 lines of vanilla JavaScript. It is served by FastAPI's `StaticFiles` mount and runs entirely in the customer's browser.

### Token Handling

The JWT arrives as a `?token=` query parameter. It is kept in a JavaScript variable in memory only — never written to `localStorage` or cookies. When the page closes, the token is gone.

### Loading State

On page load, the JS calls `GET /api/onboard/context?token=...`. While that fetch is in flight, a spinner is shown. If the response is OK, the form renders. If the token is invalid, expired, or already used, an error message is shown instead with no form fields.

### Product Field Rendering

If the customer purchased only one product, the product is shown as a read-only label. If they purchased multiple, a `<select>` dropdown is rendered and the form fields adapt when the user changes the selection (Postgres URL vs Google Sheets ID + credentials).

### Database Type Badge

The badge ("PostgreSQL" or "Google Sheets") is always read-only and derived from the selected product's `db_type`. The customer cannot change it because the backend rejects any submission whose `db_type` does not match the product's authoritative type in `purchased_products`.

### Field Validation

The Postgres URL field shows a hint to use the Supabase Transaction Pooler on port 6543 (because tenant Postgres pools run with `statement_cache_size=0` for transaction-pooler compatibility). The Google Sheets section validates the credentials JSON on blur — if it fails `JSON.parse()`, an inline error appears.

### Submission

On submit, the JS builds a body with the token, product slug, db_type, and credentials, then POSTs to `/api/onboard/submit`. The submit button shows "Submitting…" and is disabled while the request is in flight. On success, the form is replaced with a green check icon and a personalised confirmation. On failure, the server's error message (e.g. "Authentication failed", "Connection timeout") is shown above the submit button so the customer can correct and retry.

### HTML Escaping

All values from the API (`contact_name`, `company_name`, `display_name`, `slug`) are passed through an `escHtml()` helper before being inserted into the DOM, preventing XSS even if the registered client data were tampered with.

---

## 6. Caching and Performance

| Cache | What It Holds | TTL | Why It Exists |
|---|---|---|---|
| `_runtime_schema_cache` | Live Postgres schema per credential | 5 minutes | Avoids 170+ introspection queries on every message |
| `_sheets_data_cache` | Live Google Sheets rows per sheet | 60 seconds | Avoids hitting Google API on every message |
| `_embedding_cache` | OpenAI embeddings per text | LRU 500 | Skips redundant embedding API calls |
| `_tenant_pools` | asyncpg connection pools per credential | Until error | Avoids reconnecting on every query |
| `runtime_memory.json` | Learned intent rules and format patterns | Persistent | Skips redundant LLM intent calls |
| `tenant_query_examples` | Past successful (question, SQL) pairs | Permanent | Powers few-shot retrieval for better SQL accuracy |

The combination keeps the typical hot-path latency under 2 seconds: ~100ms for tenant lookup, ~400ms for parallel schema fetch and embedding, ~800ms for SQL generation, ~200ms for query execution, ~400ms for response formatting.

---

## 7. Security at a Glance

All tenant database credentials are encrypted at rest with Fernet symmetric encryption using `FERNET_SECRET_KEY`. They are decrypted only in memory when a query needs to run.

The SQL sanitiser in `app/db/security.py` rejects anything that is not a single SELECT — no INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, CREATE, GRANT, REVOKE, or multi-statement queries. `SELECT *` is gated by a separate `allow_select_star` flag.

EXPLAIN validation acts as a second layer of defence: even valid-looking SQL referencing non-existent columns is rejected before it runs.

Admin endpoints require `x-admin-token` matching `ADMIN_SECRET_TOKEN`. Onboarding endpoints use short-lived (30-minute) single-use JWTs signed with `ONBOARDING_JWT_SECRET`. Once a token's `used_at` is set, it can never be used again.
