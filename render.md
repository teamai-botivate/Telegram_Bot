# Render Deployment Documentation

This document explains how the Botivate Bot backend is deployed to Render — from repository connection to production traffic. It assumes you have a GitHub repository ready and a Render account.

---

## 1. Why Render

Render is a managed cloud platform that handles container builds, HTTPS certificates, automatic deploys from Git, environment variable management, and zero-downtime deploys. For a FastAPI app like Botivate Bot, it requires no Dockerfile and no infrastructure configuration — Render reads the `Procfile` (or a custom start command) and the `requirements.txt` and handles the rest.

The Render deployment hosts the bot's webhook endpoints (where Telegram and WhatsApp send incoming messages), the admin API (for tenant management), the onboarding API (for the self-service form), and the static onboarding form HTML.

---

## 2. Prerequisites

Before you begin, make sure you have:

1. A GitHub repository containing the Botivate Bot code.
2. A NeonDB Postgres database created (this will be the meta database — see `environment.md` for setup details).
3. A Telegram bot token from `@BotFather` (see `environment.md`).
4. An OpenAI API key with available credits.
5. A Fernet secret key generated locally — run `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` to produce one.
6. A strong random string for `ADMIN_SECRET_TOKEN` and another for `ONBOARDING_JWT_SECRET`. Use `openssl rand -hex 32` to generate each.
7. (Optional) A Supabase project for the Botivate Main Database, and WhatsApp Business API credentials.

---

## 3. Creating the Render Web Service

Log into the Render dashboard at `https://dashboard.render.com` and click **New +** → **Web Service**.

### Connect the Repository

Select **Build and deploy from a Git repository** and connect your GitHub account if you have not done so before. Pick the `botivate-bot` repository and the branch you want to deploy (typically `main`).

### Configure the Service

Fill in the following fields:

- **Name** — something like `botivate-bot` (this becomes part of your URL: `botivate-bot.onrender.com`).
- **Region** — pick the region closest to your customers. Most Indian users should use **Singapore** for lowest latency.
- **Branch** — `main`.
- **Root Directory** — leave blank (the repo root is the working directory).
- **Runtime** — **Python 3**.
- **Build Command** — `pip install -r requirements.txt`
- **Start Command** — `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

The start command matches what is in the `Procfile`. Render uses the `$PORT` environment variable, which it sets automatically to the port the service must bind to. The host must be `0.0.0.0`, not `127.0.0.1`, so the service is reachable from outside the container.

### Choose the Instance Type

For development or low-traffic use, pick the **Free** or **Starter** plan. For production, use **Standard** or higher because free instances spin down after 15 minutes of inactivity, which means the first message after idle time waits ~30 seconds for a cold start. Spinning down also clears all in-memory caches (schema, embeddings, pools) so subsequent messages are slower until caches warm up.

---

## 4. Environment Variables

In the Render dashboard, go to the **Environment** tab of your service and add each variable. Details on how to obtain each value are in `environment.md`.

### Required Core Variables

| Key | Example Value |
|---|---|
| `DATABASE_URL` | `postgresql://user:pass@ep-xyz.neon.tech/botivate_meta?sslmode=require` |
| `FERNET_SECRET_KEY` | Output from `Fernet.generate_key()` |
| `ADMIN_SECRET_TOKEN` | Random 32-byte hex string |
| `TELEGRAM_BOT_TOKEN` | Token from `@BotFather` |
| `OPENAI_API_KEY` | `sk-proj-...` from `platform.openai.com` |

### LLM Configuration

| Key | Suggested Value |
|---|---|
| `SQL_GENERATION_MODEL` | `gpt-4.1` |
| `FAST_LLM_PROVIDER` | `openai`, `groq`, or `cerebras` |
| `FAST_LLM_API_KEY` | Provider API key (skip if using OpenAI) |
| `FAST_LLM_MODEL` | `gpt-4.1-mini`, `llama-3.3-70b-versatile`, etc. |

### Onboarding

| Key | Example Value |
|---|---|
| `ONBOARDING_JWT_SECRET` | Random 32-byte hex string |
| `ONBOARDING_BASE_URL` | `https://botivate-bot.onrender.com` |

The `ONBOARDING_BASE_URL` must be the exact public URL of your Render service. The bot will use this to construct the self-service form link.

### Main DB Sync (optional but recommended for production)

| Key | Example Value |
|---|---|
| `BOTIVATE_MAIN_DB_URL` | Supabase pooler URL |
| `BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES` | `15` |

### WhatsApp (optional)

| Key | Example Value |
|---|---|
| `WHATSAPP_TOKEN` | From Meta Developer dashboard |
| `WHATSAPP_PHONE_NUMBER_ID` | From Meta Developer dashboard |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | From Meta Developer dashboard |
| `WEBHOOK_VERIFY_TOKEN` | Any random string you choose |

### Tuning (all optional, sensible defaults exist)

`SQL_DEFAULT_ROW_LIMIT`, `SQL_FULL_ROW_LIMIT`, `RUNTIME_SCHEMA_CACHE_TTL_SECONDS`, `SHEETS_CACHE_TTL_SECONDS`, `TENANT_DB_CONNECT_TIMEOUT_SECONDS`, `ENABLE_QUERY_LEARNING`.

---

## 5. Health Check Configuration

In the **Settings** tab of your Render service, set the **Health Check Path** to `/health`. The app's `/health` endpoint returns `200 OK` immediately without touching the database, so Render's heartbeat probes never time out even if the database is temporarily unavailable.

The app's startup sequence is also designed to never block ASGI startup on database availability — `create_tables()` runs in a background task bounded by `STARTUP_DB_INIT_TIMEOUT_SECONDS` (default 15 seconds). This means the service comes up and answers health checks immediately, then connects to the database in the background.

---

## 6. Deploying

Once everything is configured, click **Create Web Service**. Render will:

1. Clone your repository.
2. Run the build command (`pip install -r requirements.txt`). This usually takes 1–3 minutes the first time and is cached on subsequent deploys.
3. Run the start command (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`).
4. Wait for the health check at `/health` to return 200.
5. Route traffic to the new instance and gracefully shut down the old one (zero-downtime deploy).

You can watch the deploy logs in real time from the dashboard. Look for `Application startup complete` from Uvicorn and `INFO: Uvicorn running on http://0.0.0.0:10000` to confirm the service is healthy.

---

## 7. Running Database Migrations

Render does not run Alembic migrations automatically on deploy. After any deploy that includes a new migration, open the Render service's **Shell** tab and run:

```bash
alembic upgrade head
```

This applies all pending migrations to the database pointed to by `DATABASE_URL`. The shell shares the service's environment variables so no extra configuration is needed.

For the very first deploy, also run `alembic upgrade head` immediately after the service comes up to create all the meta database tables.

---

## 8. Registering Webhooks

Once the service is live, you need to tell Telegram and WhatsApp where to deliver messages.

### Telegram

Open a terminal anywhere with `curl` available and run:

```bash
curl "https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<your-service>.onrender.com/webhook/telegram"
```

Telegram returns `{"ok":true,"result":true,"description":"Webhook was set"}` on success. From now on, every message sent to your bot is POSTed to your Render service.

You can verify the registration by visiting `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` in a browser — it should show the URL and zero pending update errors.

### WhatsApp (Meta Cloud API)

In the Meta Developer dashboard, open your WhatsApp app's configuration:

1. Go to **Configuration → Webhook**.
2. Click **Edit** and set the callback URL to `https://<your-service>.onrender.com/webhook/whatsapp`.
3. Set the verify token to the exact value of `WEBHOOK_VERIFY_TOKEN` from Render.
4. Click **Verify and Save** — Meta will GET your endpoint with the verify token and expect the right challenge back.
5. Subscribe to the **messages** webhook field.

Send a test message from a connected WhatsApp number to confirm delivery.

---

## 9. Auto-Deploy on Push

Render is configured by default to redeploy automatically whenever you push to the connected branch. Each push triggers the build and deploy sequence; the old instance keeps serving traffic until the new one is healthy. If you want to pause auto-deploys (e.g. during an incident), toggle **Auto-Deploy** off in the service settings.

For manual deploys without a push, click **Manual Deploy → Deploy latest commit** in the dashboard.

---

## 10. Logs and Monitoring

The **Logs** tab in the Render dashboard shows live `stdout` and `stderr` from your service. The app's logging configuration in `app/main.py` filters out health-check noise so the production log is clean and focused on real activity.

Useful log markers to grep for:

- `[SQL_OUT]` — A SQL query was generated and is about to execute.
- `[SQL_OK]` — A query succeeded, with the row count.
- `[SQL_ERR]` — A query failed; check the next log line for the EXPLAIN error and the retry attempt number.
- `[SCHEMA_CACHE] HIT` / `MISS` — Schema cache behaviour, useful for performance debugging.
- `[ROUTE]` — Multi-credential routing decisions.
- `[ONBOARD]` — Onboarding form submissions, including connection test failures.
- `[QUERY_LEARNING]` — Few-shot example storage events.

For longer-term log retention, you can configure log streaming in Render's settings to forward to Datadog, Logtail, Papertrail, or any syslog endpoint.

---

## 11. Custom Domains

By default your service is reachable at `https://<service-name>.onrender.com`. For a branded URL, add a custom domain under **Settings → Custom Domains**.

1. Add the domain (e.g. `bot.botivate.in`).
2. Render shows you a CNAME target. Add the CNAME record at your DNS provider pointing to that target.
3. Render automatically issues a TLS certificate via Let's Encrypt within a few minutes.

After the domain is live, update `ONBOARDING_BASE_URL` to the new domain and re-register the Telegram and WhatsApp webhooks to use it.

---

## 12. Rollback

If a deploy breaks something, go to the **Events** tab in the Render dashboard, find the previous good deploy in the list, and click **Rollback to this deploy**. Render will redeploy from that commit immediately, bypassing the broken commit on the branch.

This does not revert the database — if the broken commit ran a migration, you may also need to manually run `alembic downgrade -1` in the shell.

---

## 13. Scaling

Render's Standard and higher plans support horizontal scaling via the **Instances** setting. Increasing instances adds replicas behind a load balancer. Important caveats for Botivate Bot:

- **In-memory caches are per-instance.** Each replica maintains its own schema cache, embedding cache, and connection pool. The first message to each replica is slower until its caches warm up.
- **Runtime memory (`runtime_memory.json`) is local.** Learning happens per replica and is not shared. For shared learning, move it to a Redis or database-backed store (not currently implemented).
- **Conversation context is per-instance.** A user whose request hits a different replica on a follow-up message will not have prior turn context. For multi-replica deployments, move context to Redis.

For most current use cases, a single Standard instance is more than enough. Only scale out when you see sustained CPU above 70% or latency degradation.

---

## 14. Cost Notes

- Render Free tier sleeps after 15 minutes of idle and is unsuitable for production (cold starts ruin the user experience).
- Render Starter (~$7/month) keeps the service warm but has limited CPU; suitable for early stage.
- Render Standard (~$25/month) gives you a dedicated CPU and is the recommended production tier.
- NeonDB has a generous free tier; the meta database is unlikely to outgrow it for a long time.
- OpenAI costs scale with usage. Switching `FAST_LLM_PROVIDER` to Groq (free tier with rate limits) or Cerebras can cut formatting and routing costs to near zero while keeping SQL generation on OpenAI for quality.
