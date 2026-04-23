# botivate-bot

Botivate bot backend with a platform-agnostic core and a bring-your-own-database model.

## Project Overview

The application normalizes incoming platform payloads into a shared BotMessage object and runs one common pipeline:
intent classification -> tenant lookup -> tenant SQL template lookup -> tenant database query -> LLM response formatting -> platform-specific reply sender.

Platform channels:
- Telegram: active sender and webhook endpoint.
- WhatsApp: webhook endpoint and sender stub are present, ready for activation when credentials are available.

Database model:
- Botivate meta database (NeonDB): stores tenants, encrypted tenant DB credentials, and intent-to-SQL schema maps.
- Tenant business databases: owned by customer organizations and queried on demand using decrypted credentials.

## Local Setup

1. Clone the repository.
2. Create your environment file:

	cp .env.example .env

3. Install dependencies:

	pip install -r requirements.txt

4. Run the app locally:

	uvicorn app.main:app --reload

5. Seed demo meta data (optional but recommended):

	python seed_data.py

## Webhook Endpoints

- Telegram webhook: POST /webhook/telegram
- WhatsApp webhook (messages): POST /webhook/whatsapp
- WhatsApp webhook verification: GET /webhook/whatsapp

Both POST webhooks are implemented to always respond with HTTP 200.

## Admin Endpoints

These endpoints are protected by header x-admin-token with ADMIN_SECRET_TOKEN value.

- POST /admin/tenant/connect-db
	- Tests tenant DB connection
	- Encrypts credentials with Fernet
	- Saves TenantDBCredential

- POST /admin/tenant/schema-map
	- Stores tenant-specific SQL template by module + intent
	- Upserts TenantSchemaMap

- POST /admin/tenant/{tenant_id}/refresh-schema
	- Re-runs PostgreSQL schema introspection
	- Updates stored `schema_blueprint` for that tenant

## Render Deployment

1. Create a new Web Service in Render and connect this repository.
2. Set these environment variables in the Render dashboard:
	DATABASE_URL, FERNET_SECRET_KEY, ADMIN_SECRET_TOKEN, TELEGRAM_BOT_TOKEN, MISTRAL_API_KEY, OPENAI_API_KEY, SQL_GENERATION_MODEL, RESPONSE_FORMAT_MODEL, SQL_DEFAULT_ROW_LIMIT, SQL_FULL_ROW_LIMIT

	When ready to enable WhatsApp, also set:
	WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_BUSINESS_ACCOUNT_ID, WEBHOOK_VERIFY_TOKEN
3. Set build command:

	pip install -r requirements.txt

4. Set start command:

	uvicorn app.main:app --host 0.0.0.0 --port $PORT

## Tenant Onboarding Flow

1. Create or identify tenant record in meta DB.
2. Call POST /admin/tenant/connect-db with tenant DB credentials.
3. Call POST /admin/tenant/schema-map once per module+intent to store that tenant's SQL templates.
4. Tenant can now ask questions; Botivate executes mapped SQL against the tenant's own DB.

## WhatsApp Activation

The WhatsApp sender is intentionally stubbed in app/platforms/whatsapp.py.

To activate WhatsApp:
1. Populate WhatsApp credentials in your .env or Render environment.
2. Replace the stub implementation with the commented httpx implementation in app/platforms/whatsapp.py.

No changes are needed in app/bot_logic.py because the core stays platform-agnostic.

## Register Webhook with Meta Cloud API

1. In the Meta Developer dashboard, open your WhatsApp app configuration.
2. Set the callback URL to your deployed endpoint:
	https://<your-render-service>.onrender.com/webhook/whatsapp
3. Set the verify token to exactly match WEBHOOK_VERIFY_TOKEN.
4. Subscribe to the messages webhook field.
5. Send a test message from a connected WhatsApp number to confirm webhook delivery.

## Register Webhook with Telegram

Set Telegram webhook URL to:
https://<your-render-service>.onrender.com/webhook/telegram

## Switch from Mistral to Claude API

Open app/bot_logic.py and find the comment # SWITCH_TO_CLAUDE.
Replace only the API call block at that marker (request URL, headers, and payload/response parsing) to switch providers while keeping the rest of the message flow unchanged.

## SQL Generation Pipeline

- SQL generation uses GPT (`SQL_GENERATION_MODEL`, default `gpt-4.1`) via OpenAI SDK.
- Reply formatting uses Mistral small (`mistral-small-latest`).
- Tenant-specific schema blueprint is stored in `tenant_db_credentials.schema_blueprint`.
- Schema blueprint can be refreshed on demand with:
	- `POST /admin/tenant/{tenant_id}/refresh-schema`
- SQL execution includes self-healing retry logic:
	- On malformed SQL / PostgreSQL errors, the bot calls `fix_sql()` and retries automatically (up to 2 retries).

## Project Structure

```text
botivate-bot/
├── app/
│   ├── __init__.py
│   ├── admin.py
│   ├── main.py
│   ├── webhook.py
│   ├── platforms/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── telegram.py
│   │   └── whatsapp.py
│   ├── bot_logic.py
│   ├── database.py
│   └── models.py
├── tests/
├── .env.example
├── .gitignore
├── requirements.txt
├── alembic.ini
├── alembic/
├── seed_data.py
├── Procfile
└── README.md
```
