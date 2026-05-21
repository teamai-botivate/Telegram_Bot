# Environment Variables — Step-by-Step Setup Guide

This document explains how to obtain every environment variable required by Botivate Bot. Each section walks you through the actual UI of the relevant service (Telegram, WhatsApp, NeonDB, Supabase, OpenAI, etc.) so you can collect each value from scratch.

When you are done, your `.env` file should contain all the variables listed at the end of this document. The same values should also be set in the Render dashboard for production deployment.

---

## 1. Telegram

### `TELEGRAM_BOT_TOKEN`

This is the token that authenticates your backend with Telegram's API.

**Steps to obtain:**

1. Open Telegram and search for the user `@BotFather`. This is Telegram's official bot for creating other bots.
2. Send `/start` to begin, then send `/newbot`.
3. BotFather will ask for a display name for your bot. Type something like `Botivate Bot`. This is what users see in their chat list.
4. Next, BotFather asks for a username. It must end in `bot` (e.g. `botivate_assistant_bot`). Usernames must be globally unique across Telegram.
5. Once the bot is created, BotFather replies with a message containing your token in the form `123456789:AAH8XnQ4-abc...`. This is your `TELEGRAM_BOT_TOKEN`.
6. (Optional) Send `/setdescription`, `/setabouttext`, and `/setuserpic` to customise how your bot appears.
7. (Optional but recommended) Send `/setcommands` to register the slash commands users can see in the menu: `start`, `help`, and `adddb`.

Copy the token into your `.env` file. Never commit this token to git — anyone with the token can impersonate your bot.

### User's Telegram Chat ID (`telegram_chat_id` per tenant)

The user's Telegram chat ID is not a backend env var — it is stored per tenant in the database. But the customer needs to provide it during onboarding. Here is how a customer can find their own chat ID:

1. The customer opens Telegram and searches for `@userinfobot` (or `@getidsbot`).
2. They send `/start` to it.
3. The bot replies with their numeric chat ID, e.g. `123456789`.

In normal operation, the customer's chat ID is captured automatically the first time they message the Botivate Bot — the webhook payload includes `message.chat.id` which the bot stores. The Botivate team only needs to ask for it manually if they are creating tenants through the admin API rather than the self-service flow.

### Telegram Webhook Registration

After deploying to Render, register your webhook URL with Telegram so messages are delivered:

```bash
curl "https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<your-render-service>.onrender.com/webhook/telegram"
```

A successful response looks like `{"ok":true,"result":true,"description":"Webhook was set"}`. To verify, visit `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` in a browser.

---

## 2. WhatsApp (Meta Cloud API)

WhatsApp Business messaging is provided by Meta's Cloud API. You need a Meta Developer account and a verified WhatsApp Business phone number.

### `WHATSAPP_TOKEN`

This is a permanent access token for the WhatsApp Cloud API.

**Steps to obtain:**

1. Go to `https://developers.facebook.com` and sign in.
2. Create a new app: **My Apps → Create App → Business**.
3. Add the **WhatsApp** product to your app from the product list on the left sidebar.
4. Open **WhatsApp → API Setup**. You will see a temporary 24-hour token at the top — this works for development but expires too quickly for production.
5. For a permanent token, go to **Business Settings → System Users**, create a new system user with **Admin** access, and click **Generate New Token**. Select your app, set the token to **Never expire**, and grant the `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
6. Copy the generated token. This is your `WHATSAPP_TOKEN`.

### `WHATSAPP_PHONE_NUMBER_ID`

This is the ID of the phone number that will send messages.

**Steps to obtain:**

1. In the Meta Developer dashboard, go to **WhatsApp → API Setup**.
2. Under the **From** dropdown, you'll see your test phone number and its ID.
3. The ID is a long numeric string displayed below the dropdown, labelled **Phone number ID**. Copy that value.
4. For production, you must add a real business phone number under **WhatsApp → Phone Numbers** and verify it via SMS or voice call. The verified number's ID then replaces the test ID.

### `WHATSAPP_BUSINESS_ACCOUNT_ID`

This is the ID of the WhatsApp Business Account that owns your phone number.

**Steps to obtain:**

1. In Meta Business Suite (`business.facebook.com`), go to **Settings → Business Assets → WhatsApp Accounts**.
2. Select your WhatsApp Business Account.
3. The numeric ID is shown in the top-right corner, or in the URL. Copy it.

### `WEBHOOK_VERIFY_TOKEN`

This is a string **you choose** to verify Meta's webhook handshake. It is not issued by Meta — you make it up and put the same value in both the Render env vars and the Meta dashboard.

**Steps:**

1. Generate a random string, e.g. `openssl rand -hex 24` produces something like `a3f1b9c2e7d4a8f5b1c9e2d7a4f1b8c5`.
2. Set this as `WEBHOOK_VERIFY_TOKEN` in your `.env` and Render.
3. In the Meta Developer dashboard, go to **WhatsApp → Configuration → Webhook**.
4. Click **Edit**. Set the **Callback URL** to `https://<your-render-service>.onrender.com/webhook/whatsapp` and paste the same verify token into the **Verify token** field.
5. Click **Verify and Save**. Meta will send a GET request to your endpoint with `hub.mode=subscribe`, `hub.verify_token=<your token>`, and `hub.challenge=<random>`. The backend (`app/webhook.py`) returns the challenge if the token matches.
6. Subscribe to the **messages** webhook field so inbound messages are delivered.

---

## 3. Meta / Botivate Main Database (Supabase)

The Botivate Main Database is the Supabase Postgres instance that holds the master list of all paying customers (orders/sales records sync into the `registered_clients` table on the bot's meta database from here).

### `BOTIVATE_MAIN_DB_URL`

**Steps to obtain:**

1. Sign in to Supabase at `https://supabase.com/dashboard`.
2. Open the project that holds the Botivate Main Database.
3. Go to **Project Settings → Database**.
4. Scroll to **Connection string** and select the **URI** tab.
5. There are two options:
   - **Direct connection** — `db.xxxxx.supabase.co:5432`. Good for low-traffic admin tasks.
   - **Transaction pooler** — `aws-0-...pooler.supabase.com:6543`. Recommended for production because Supabase free tier limits direct connections.
6. Copy the URI. It looks like: `postgresql://postgres.xxxxx:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:6543/postgres`.
7. Replace `[YOUR-PASSWORD]` with the actual database password (set when the project was first created — if forgotten, reset it in the same Database settings page).

Use this URI as `BOTIVATE_MAIN_DB_URL`. The sync scheduler is silently disabled if this variable is not set, so you can develop without it.

### `BOTIVATE_MAIN_DB_QUERY` (optional)

The SQL query the sync job runs to fetch client records. Defaults to `SELECT * FROM clients WHERE is_active = TRUE`. Override if your table is named differently or you need a join.

### `BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES` (optional)

How often (in minutes) to re-sync. Default is `15`. For production, 15 minutes is a good balance between freshness and load on the Supabase database.

### `BOTIVATE_MAIN_DB_CONNECT_TIMEOUT_SECONDS` (optional)

Connect timeout for the sync job. Default is `30`.

---

## 4. Meta Database — NeonDB

NeonDB is the serverless Postgres provider for Botivate's own meta database. This database holds tenants, encrypted credentials, query examples, registered clients, and onboarding tokens.

### `DATABASE_URL`

**Steps to obtain:**

1. Sign in at `https://console.neon.tech`.
2. Click **New Project**. Pick a name like `botivate-meta`, the **PostgreSQL 16** version, and the region closest to your Render deployment (typically Singapore or Mumbai for Indian users).
3. After the project is created, you land on the **Dashboard**. The connection string is shown at the top under **Connection Details**.
4. Choose **Pooled connection** for production (it uses Neon's PgBouncer pool, which handles high concurrency well).
5. Copy the connection string. It looks like:
   ```
   postgresql://botivate_user:AbCdEf123@ep-cool-name-12345-pooler.ap-southeast-1.aws.neon.tech/botivate_meta?sslmode=require
   ```
6. **Important**: enable the `pgvector` extension. In the Neon console, open **SQL Editor** and run:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
   This is required because the `tenant_query_examples` table uses a `vector(1536)` column for embedding-based few-shot retrieval. Migrations will fail without it.

Use this URI as `DATABASE_URL`. After setting it in your environment, run `alembic upgrade head` to create all the meta database tables.

---

## 5. OpenAI

### `OPENAI_API_KEY`

This key authenticates SQL generation and embedding requests with OpenAI.

**Steps to obtain:**

1. Go to `https://platform.openai.com` and sign in (or create an account).
2. Open **Settings → API Keys** in the left sidebar.
3. Click **Create new secret key**. Give it a descriptive name like `botivate-bot-prod`.
4. (Recommended) Set permissions to **All** initially; you can scope down later.
5. Click **Create secret key**. The key (starting with `sk-proj-...` or `sk-...`) is shown once — copy it immediately. OpenAI does not let you view it again later.
6. Add billing if you haven't already, under **Settings → Billing → Payment methods**. Without an active payment method, your key returns 429 errors.

Use the key as `OPENAI_API_KEY`. Monitor usage on the **Usage** page; SQL generation and embeddings together typically cost $0.001–$0.005 per question depending on schema size and result size.

### `SQL_GENERATION_MODEL` (optional)

Defaults to `gpt-4.1`. Recommended values: `gpt-4.1` (best quality), `gpt-4o` (slightly cheaper, near-equivalent quality), or `gpt-4o-mini` (cheapest but with measurable quality loss on complex queries).

### `EMBEDDING_MODEL` (optional)

Defaults to `text-embedding-3-small` (1536 dimensions, very cheap). Do not change this without also updating the `vector(1536)` column type in `tenant_query_examples`.

---

## 6. Fast LLM Provider (Optional Cost Optimisation)

The "fast LLM" is used for intent classification, multi-database routing, and response formatting — tasks that benefit from speed and don't need maximum quality. By default it uses OpenAI's `gpt-4.1-mini`, but you can route it to Groq or Cerebras for free / faster inference.

### `FAST_LLM_PROVIDER`

One of `openai`, `groq`, or `cerebras`. Default `openai`.

### Groq Setup

1. Go to `https://console.groq.com` and sign in.
2. Click **API Keys → Create API Key**.
3. Copy the key (starts with `gsk_`).
4. Set `FAST_LLM_PROVIDER=groq`, `FAST_LLM_API_KEY=gsk_...`, `FAST_LLM_MODEL=llama-3.3-70b-versatile`.

Groq's free tier has rate limits (30 req/min) which are fine for early stage but may bottleneck under heavy load.

### Cerebras Setup

1. Go to `https://cloud.cerebras.ai` and sign in.
2. Open **API Keys → Create Key**.
3. Copy the key (starts with `csk-`).
4. Set `FAST_LLM_PROVIDER=cerebras`, `FAST_LLM_API_KEY=csk-...`, `FAST_LLM_MODEL=llama-3.3-70b`.

Cerebras offers extremely fast inference (often under 200ms for short prompts) and is well-suited for the formatting step.

---

## 7. Encryption and Security Secrets

### `FERNET_SECRET_KEY`

This key encrypts and decrypts tenant database credentials at rest in the meta database. Losing or rotating this key without re-encrypting all existing credentials will make stored credentials permanently unreadable.

**Steps to generate:**

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The output is a 44-character base64 string ending in `=`. Set it as `FERNET_SECRET_KEY` and **never change it** without a planned rotation procedure (decrypt-with-old → re-encrypt-with-new for every credential row).

### `ADMIN_SECRET_TOKEN`

This is the shared secret used in the `x-admin-token` header to authenticate calls to `/admin/*` endpoints.

**Steps to generate:**

```bash
openssl rand -hex 32
```

The output is a 64-character hex string. Set it as `ADMIN_SECRET_TOKEN`. Keep it secret; treat it like a root password.

### `ONBOARDING_JWT_SECRET`

This signs the single-use JWTs used by the self-service onboarding flow. It must be a long, random string.

**Steps to generate:**

```bash
openssl rand -hex 32
```

Set it as `ONBOARDING_JWT_SECRET`. It is independent of `ADMIN_SECRET_TOKEN` and should be a different value.

### `ONBOARDING_BASE_URL`

The base URL where the self-service form is served. Must be the exact public URL of your Render service (or your custom domain). The bot constructs onboarding links of the form `<ONBOARDING_BASE_URL>/static/onboard.html?token=<jwt>`.

Example: `https://botivate-bot.onrender.com` or `https://bot.botivate.in`.

---

## 8. Tuning Variables (All Optional)

| Variable | Default | What It Does |
|---|---|---|
| `SQL_DEFAULT_ROW_LIMIT` | `50` | Default `LIMIT` applied to SQL queries when the user does not ask for "all". |
| `SQL_FULL_ROW_LIMIT` | `500` | Max rows returned when the user asks for full data. |
| `RUNTIME_SCHEMA_CACHE_TTL_SECONDS` | `300` | How long to cache tenant Postgres schema introspection results in memory. |
| `SHEETS_CACHE_TTL_SECONDS` | `60` | How long to cache Google Sheets row data. |
| `TENANT_DB_CONNECT_TIMEOUT_SECONDS` | `30` | Per-connection timeout for tenant databases. |
| `TENANT_DB_CONNECT_RETRIES` | `2` | Retry count on tenant DB connection errors. |
| `STARTUP_DB_INIT_TIMEOUT_SECONDS` | `15` | How long to wait for meta DB table creation during startup. |
| `ENABLE_QUERY_LEARNING` | `true` | If true, store successful (question, SQL) pairs for few-shot retrieval. |

---

## 9. Putting It All Together — Example `.env`

After collecting everything above, your `.env` file should look roughly like this:

```bash
# ── Core ─────────────────────────────────────────────────────────────────
DATABASE_URL=postgresql://botivate_user:xxxx@ep-xyz-pooler.aws.neon.tech/botivate_meta?sslmode=require
FERNET_SECRET_KEY=k1Q3...==
ADMIN_SECRET_TOKEN=64charhexstring...
TELEGRAM_BOT_TOKEN=123456789:AAH...
OPENAI_API_KEY=sk-proj-...

# ── LLM Models ──────────────────────────────────────────────────────────
SQL_GENERATION_MODEL=gpt-4.1
FAST_LLM_PROVIDER=openai
FAST_LLM_MODEL=gpt-4.1-mini
EMBEDDING_MODEL=text-embedding-3-small

# ── Onboarding ──────────────────────────────────────────────────────────
ONBOARDING_JWT_SECRET=anotherhex...
ONBOARDING_BASE_URL=https://botivate-bot.onrender.com

# ── Botivate Main DB Sync (Supabase) ────────────────────────────────────
BOTIVATE_MAIN_DB_URL=postgresql://postgres.xxxx:password@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES=15

# ── WhatsApp (optional) ─────────────────────────────────────────────────
WHATSAPP_TOKEN=EAAB...
WHATSAPP_PHONE_NUMBER_ID=1234567890
WHATSAPP_BUSINESS_ACCOUNT_ID=9876543210
WEBHOOK_VERIFY_TOKEN=randomstring...

# ── Tuning (optional, defaults shown) ───────────────────────────────────
SQL_DEFAULT_ROW_LIMIT=50
SQL_FULL_ROW_LIMIT=500
RUNTIME_SCHEMA_CACHE_TTL_SECONDS=300
SHEETS_CACHE_TTL_SECONDS=60
ENABLE_QUERY_LEARNING=true
```

---

## 10. Verifying Your Setup

After setting all variables, you can sanity-check each integration with these commands.

### Verify Telegram

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
```

Should return your bot's info. A `401 Unauthorized` means the token is wrong.

### Verify OpenAI

```bash
curl https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" | head -20
```

Should return a JSON list of available models.

### Verify NeonDB

```bash
psql "$DATABASE_URL" -c "SELECT version();"
```

Should print the PostgreSQL version. Then verify pgvector is installed:

```bash
psql "$DATABASE_URL" -c "SELECT extname FROM pg_extension WHERE extname = 'vector';"
```

Should return one row. If empty, run `CREATE EXTENSION vector;`.

### Verify Supabase (Botivate Main DB)

```bash
psql "$BOTIVATE_MAIN_DB_URL" -c "SELECT COUNT(*) FROM clients;"
```

Should return the count of client records.

### Verify the Bot End-to-End

After deploying to Render and registering the Telegram webhook, send `/start` to your bot from any Telegram account. You should receive a welcome message within a few seconds. If you have already been registered as a client in the Botivate Main Database, you will receive a self-service onboarding link.

---

## 11. Rotating Secrets

If a secret leaks or you want to rotate periodically:

- **`TELEGRAM_BOT_TOKEN`** — message `@BotFather`, send `/revoke`, pick your bot, get a new token. Old token is invalidated immediately.
- **`OPENAI_API_KEY`** — revoke the old key in the OpenAI dashboard and create a new one. Update Render's env var. Old key stops working immediately.
- **`ADMIN_SECRET_TOKEN`** and **`ONBOARDING_JWT_SECRET`** — generate new values and update Render. Existing onboarding links signed with the old JWT secret become invalid; reissue them as needed.
- **`FERNET_SECRET_KEY`** — **do not rotate without a migration plan**. Every encrypted credential row must be decrypted with the old key and re-encrypted with the new key inside a single migration.
- **`WHATSAPP_TOKEN`** — regenerate from the Meta Developer dashboard. Old token is revoked immediately.

After rotating any secret, redeploy on Render so the new value is picked up (Render automatically restarts the service when env vars change).
