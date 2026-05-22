from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, RegisteredClient, Tenant, TenantDBCredential

from .core import *
from .core import _runtime_schema_cache
from .connection import _resolve_tenant_dsn, _get_pool_for_tenant, _get_pool_for_credential, _evict_tenant_pool, _convert_to_asyncpg_url, _quote_ident, _describe_connection_exception
from .security import _decrypt_credential_value, _sanitize_select_sql
from .crud import get_tenant_credentials
from .sheets import fetch_google_sheet_data, invalidate_sheets_data_cache
async def execute_tenant_query(
	tenant_id: uuid.UUID | str,
	sql: str,
	*params: Any,
	allow_select_star: bool = False,
) -> list[dict[str, Any]]:
	tid = str(tenant_id)

	for attempt in range(2):
		pool = await _get_pool_for_tenant(tenant_id)
		try:
			async with pool.acquire() as connection:
				safe_sql = _sanitize_select_sql(sql, allow_select_star=allow_select_star)
				logger.info(
					"Tenant query attempt tenant_id=%s timestamp=%s sql=%s",
					tenant_id,
					datetime.now(timezone.utc).isoformat(),
					safe_sql,
				)

				rows = await connection.fetch(safe_sql, *params)
				return [dict(row) for row in rows]
		except (TimeoutError, OSError, ConnectionResetError) as e:
			# Pool connection is stale — evict and retry once
			logger.warning("Pool connection failed for tenant %s (attempt %d): %s", tid, attempt + 1, e)
			await _evict_tenant_pool(tid)
			if attempt == 1:
				raise TenantDBConnectionError(f"Could not connect to tenant database after retry: {e}")
		except QueryExecutionError:
			raise
		except SecurityError:
			raise
		except asyncpg.PostgresError as e:
			logger.error(f"PostgresError executing tenant query. SQL: {sql} | Error: {e}")
			raise QueryExecutionError(f"Failed to execute query: {e}")
		except Exception as e:
			logger.error(f"Unexpected error executing tenant query: {e}")
			raise QueryExecutionError("An unexpected error occurred while running tenant query.")

async def explain_validate_sql(tenant_id: uuid.UUID | str, sql: str) -> tuple[bool, str]:
	"""Run EXPLAIN on the SQL against the tenant's database to validate structure.

	Returns (is_valid, error_message). If valid, error_message is empty.
	This catches wrong column names, wrong table names, bad joins, and
	syntax errors — without actually executing the query or touching data.
	Works with any tenant schema automatically.
	"""
	tid = str(tenant_id)
	try:
		pool = await _get_pool_for_tenant(tenant_id)
		async with pool.acquire() as connection:
			await connection.fetch(f"EXPLAIN {sql}")
		return True, ""
	except (TimeoutError, OSError, ConnectionResetError) as e:
		logger.warning("EXPLAIN connection failed for tenant %s: %s", tid, e)
		await _evict_tenant_pool(tid)
		# Connection issue, not SQL issue — let it pass through
		return True, ""
	except asyncpg.PostgresError as e:
		error_msg = str(e)
		logger.info("[EXPLAIN_FAIL] tenant=%s sql=%s error=%s", tid, sql, error_msg)
		return False, error_msg
	except Exception as e:
		logger.warning("EXPLAIN unexpected error for tenant %s: %s", tid, e)
		# Don't block on unexpected errors — let execution try
		return True, ""

async def execute_credential_query(
	credential: TenantDBCredential,
	sql: str,
	*params: Any,
	allow_select_star: bool = False,
) -> list[dict[str, Any]]:
	"""Run a SELECT against a specific credential's database.

	Mirrors execute_tenant_query() but targets one specific credential row, so it
	works for tenants with multiple DBs.
	"""
	cache_key = str(credential.id)

	for attempt in range(2):
		pool = await _get_pool_for_credential(credential)
		try:
			async with pool.acquire() as connection:
				safe_sql = _sanitize_select_sql(sql, allow_select_star=allow_select_star)
				logger.info(
					"Tenant query attempt credential_id=%s timestamp=%s sql=%s",
					credential.id,
					datetime.now(timezone.utc).isoformat(),
					safe_sql,
				)
				rows = await connection.fetch(safe_sql, *params)
				return [dict(row) for row in rows]
		except (TimeoutError, OSError, ConnectionResetError) as e:
			logger.warning("Pool connection failed for credential %s (attempt %d): %s", cache_key, attempt + 1, e)
			await _evict_tenant_pool(cache_key)
			if attempt == 1:
				raise TenantDBConnectionError(f"Could not connect to tenant database after retry: {e}")
		except QueryExecutionError:
			raise
		except SecurityError:
			raise
		except asyncpg.PostgresError as e:
			logger.error(f"PostgresError executing credential query. SQL: {sql} | Error: {e}")
			raise QueryExecutionError(f"Failed to execute query: {e}")
		except Exception as e:
			logger.error(f"Unexpected error executing credential query: {e}")
			raise QueryExecutionError("An unexpected error occurred while running tenant query.")

async def explain_validate_sql_for_credential(
	credential: TenantDBCredential, sql: str
) -> tuple[bool, str]:
	"""EXPLAIN-validate a SQL statement against a specific credential's DB."""
	cache_key = str(credential.id)
	try:
		pool = await _get_pool_for_credential(credential)
		async with pool.acquire() as connection:
			await connection.fetch(f"EXPLAIN {sql}")
		return True, ""
	except (TimeoutError, OSError, ConnectionResetError) as e:
		logger.warning("EXPLAIN connection failed for credential %s: %s", cache_key, e)
		await _evict_tenant_pool(cache_key)
		return True, ""
	except asyncpg.PostgresError as e:
		error_msg = str(e)
		logger.info("[EXPLAIN_FAIL] credential=%s sql=%s error=%s", cache_key, sql, error_msg)
		return False, error_msg
	except Exception as e:
		logger.warning("EXPLAIN unexpected error for credential %s: %s", cache_key, e)
		return True, ""

async def fetch_credential_postgres_runtime_schema(
	credential: TenantDBCredential,
) -> tuple[str, str]:
	"""Runtime schema introspection for a specific credential's DB.

	Results are cached per credential for RUNTIME_SCHEMA_CACHE_TTL_SECONDS
	(default 5 min) to avoid running 170+ introspection queries on every message.
	The cache is invalidated when refresh_schema_blueprint() is called.
	"""
	if credential.db_type.lower() != "postgresql":
		raise TenantDBConnectionError("Only PostgreSQL tenant databases are supported for PostgreSQL schema introspection.")

	cache_key = str(credential.id)
	now = time.monotonic()

	cached = _runtime_schema_cache.get(cache_key)
	if cached is not None:
		ts, cached_schema, cached_hints = cached
		if now - ts < RUNTIME_SCHEMA_CACHE_TTL_SECONDS:
			logger.debug("[SCHEMA_CACHE] HIT credential=%s age=%.1fs", cache_key, now - ts)
			return cached_schema, cached_hints

	logger.debug("[SCHEMA_CACHE] MISS credential=%s — running full introspection", cache_key)
	connection_url = _decrypt_credential_value(credential.connection_url)
	schema, hints = await fetch_postgres_runtime_schema(connection_url)
	_runtime_schema_cache[cache_key] = (now, schema, hints)
	return schema, hints

def invalidate_runtime_schema_cache(credential_id: uuid.UUID | str | None = None) -> None:
	"""Clear cached runtime schema. Pass a credential_id to clear one entry, or None to clear all."""
	if credential_id is not None:
		key = str(credential_id)
		if _runtime_schema_cache.pop(key, None) is not None:
			logger.info("[SCHEMA_CACHE] Invalidated credential=%s", key)
	else:
		_runtime_schema_cache.clear()
		logger.info("[SCHEMA_CACHE] Invalidated ALL entries")

async def fetch_postgres_runtime_schema(connection_string: str) -> tuple[str, str]:
	connection: asyncpg.Connection | None = None
	try:
		# asyncpg doesn't understand sslmode= in URL; strip it and pass ssl= explicitly.
		from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

		normalized_url = _convert_to_asyncpg_url(connection_string)
		parsed = urlparse(normalized_url)
		if not parsed.hostname:
			raise ValueError("Connection URL is missing a hostname.")
		if not parsed.path or parsed.path == "/":
			raise ValueError("Connection URL is missing a database name (for example: /postgres).")
		query_params = parse_qs(parsed.query)

		ssl_mode = query_params.pop("sslmode", query_params.pop("ssl", ["require"]))[0]
		clean_query = urlencode({k: v[0] for k, v in query_params.items()})
		clean_url = urlunparse(parsed._replace(query=clean_query))

		allowed_ssl_modes = {"require", "prefer", "disable", "verify-ca", "verify-full"}
		ssl_arg = ssl_mode if ssl_mode in allowed_ssl_modes else "require"

		connection = await asyncpg.connect(clean_url, ssl=ssl_arg, timeout=15, statement_cache_size=0)

		enum_sql = """
		SELECT t.typname AS enum_name, array_agg(e.enumlabel::text) AS enum_values
		FROM pg_type t
		JOIN pg_enum e ON t.oid = e.enumtypid
		GROUP BY t.typname;
		"""
		enum_rows = await connection.fetch(enum_sql)
		enums = {row["enum_name"]: row["enum_values"] for row in enum_rows}

		column_sql = """
		SELECT table_schema, table_name, column_name, data_type, udt_name, is_nullable
		FROM information_schema.columns
		WHERE table_schema = 'public'
		ORDER BY table_schema, table_name, ordinal_position;
		"""
		rows = await connection.fetch(column_sql)

		fk_sql = """
		SELECT
			tc.table_schema,
			tc.table_name,
			kcu.column_name,
			ccu.table_schema AS foreign_table_schema,
			ccu.table_name AS foreign_table_name,
			ccu.column_name AS foreign_column_name
		FROM information_schema.table_constraints tc
		JOIN information_schema.key_column_usage kcu
			ON tc.constraint_name = kcu.constraint_name
			AND tc.table_schema = kcu.table_schema
		JOIN information_schema.constraint_column_usage ccu
			ON ccu.constraint_name = tc.constraint_name
			AND ccu.table_schema = tc.table_schema
		WHERE tc.constraint_type = 'FOREIGN KEY'
			AND tc.table_schema = 'public'
		ORDER BY tc.table_schema, tc.table_name, kcu.column_name;
		"""
		fk_rows = await connection.fetch(fk_sql)

		status_keywords = {
			"submitted",
			"completed",
			"done",
			"approved",
			"closed",
			"finished",
			"resolved",
			"verified",
			"paid",
			"delivered",
			"started",
			"ended",
			"cancelled",
		}
		completion_table_keywords = {"done", "completed", "archived", "history", "log", "audit"}
		reference_column_names = {"id", "user_id", "employee_id", "created_by", "assigned_to", "given_by"}
		date_like_types = {"date", "timestamp without time zone", "timestamp with time zone"}

		tables: dict[str, dict[str, Any]] = {}
		for row in rows:
			schema = row["table_schema"]
			table = row["table_name"]
			column = row["column_name"]
			data_type = row["data_type"]
			udt_name = row["udt_name"]
			try:
				is_nullable = row["is_nullable"]
			except Exception:
				is_nullable = "YES"
			nullable = str(is_nullable).upper() == "YES"

			if data_type == "USER-DEFINED" and udt_name in enums:
				data_type = f"enum({', '.join(enums[udt_name])})"

			full_name = f"{schema}.{table}" if schema != "public" else table
			if full_name not in tables:
				tables[full_name] = {
					"schema": schema,
					"table": table,
					"columns": [],
					"column_meta": [],
					"text_columns": [],
					"bool_columns": [],
					"fks": [],
				}
			tables[full_name]["columns"].append(f"{column} ({data_type})")
			tables[full_name]["column_meta"].append(
				{
					"name": column,
					"data_type": data_type,
					"nullable": nullable,
				}
			)
			if data_type.lower() in {"text", "character varying", "character", "varchar"}:
				tables[full_name]["text_columns"].append(column)
			if data_type.lower() == "boolean":
				tables[full_name]["bool_columns"].append(column)

		fk_details: list[dict[str, str]] = []
		for fk in fk_rows:
			src_schema = fk["table_schema"]
			src_table = fk["table_name"]
			src_col = fk["column_name"]
			dst_schema = fk["foreign_table_schema"]
			dst_table = fk["foreign_table_name"]
			dst_col = fk["foreign_column_name"]

			src_full = f"{src_schema}.{src_table}" if src_schema != "public" else src_table
			dst_full = f"{dst_schema}.{dst_table}" if dst_schema != "public" else dst_table
			if src_full in tables:
				tables[src_full]["fks"].append(f"FK: {src_full}.{src_col} -> {dst_full}.{dst_col}")
			fk_details.append(
				{
					"src_table": src_full,
					"src_col": src_col,
					"dst_table": dst_full,
					"dst_col": dst_col,
				}
			)

		relationship_lines: list[str] = []
		for fk in fk_details:
			relationship_lines.append(f"{fk['src_table']}.{fk['src_col']} -> {fk['dst_table']}.{fk['dst_col']}")

		fk_by_table_col: dict[tuple[str, str], dict[str, str]] = {}
		for fk in fk_details:
			fk_by_table_col[(fk["src_table"], fk["src_col"])] = fk

		auto_hints_lines: list[str] = []

		blueprint = "Database Blueprint (PostgreSQL):\n"
		blueprint += "RELATIONSHIPS (use these for JOINs):\n"
		if relationship_lines:
			for rel in relationship_lines:
				blueprint += rel + "\n"
		else:
			blueprint += "(none)\n"
		blueprint += "\nTABLES:\n"
		for table_name, info in tables.items():
			schema = info["schema"]
			table = info["table"]
			quoted_schema = _quote_ident(schema)
			quoted_table = _quote_ident(table)

			row_count_str = "unknown"
			try:
				count_row = await connection.fetchrow(f"SELECT COUNT(*)::bigint AS cnt FROM {quoted_schema}.{quoted_table}")
				if count_row is not None:
					row_count_str = str(count_row["cnt"])
			except Exception:
				pass

			blueprint += f"Table `{table_name}` | Rows: ~{row_count_str}\n"
			blueprint += f"Columns: {', '.join(info['columns'])}\n"

			table_hint_lines: list[str] = []

			# a) Nullable status timestamps/dates
			for col_meta in info.get("column_meta", []):
				col_name = col_meta["name"]
				dtype = str(col_meta["data_type"]).lower()
				nullable = bool(col_meta.get("nullable", True))
				if not nullable:
					continue
				if dtype not in date_like_types:
					continue
				lowered = col_name.lower()
				if any(k in lowered for k in status_keywords):
					hint = (
						f"Status hint: {table_name}.{col_name} IS NULL = pending/incomplete, "
						f"IS NOT NULL = done/complete"
					)
					table_hint_lines.append(hint)

			# b) Boolean columns
			for bool_col in info.get("bool_columns", []):
				table_hint_lines.append(f"Boolean: {bool_col} -- use = TRUE or = FALSE, never ILIKE")

			for text_col in info["text_columns"]:
				samples: list[str] = []
				try:
					quoted_col = _quote_ident(text_col)
					sample_rows = await connection.fetch(
						f"SELECT DISTINCT {quoted_col} AS value FROM {quoted_schema}.{quoted_table} "
						f"WHERE {quoted_col} IS NOT NULL LIMIT 5"
					)
					samples = [str(sample["value"]) for sample in sample_rows]
					if samples:
						blueprint += f"Sample `{text_col}`: {samples}\n"

						# c) Enum-like text columns (best-effort): count distinct values up to 10.
						try:
							cnt_row = await connection.fetchrow(
								f"SELECT COUNT(*)::int AS cnt FROM ("
								f"SELECT DISTINCT {quoted_col} AS v FROM {quoted_schema}.{quoted_table} "
								f"WHERE {quoted_col} IS NOT NULL LIMIT 10"
								f") s"
							)
							cnt = int(cnt_row["cnt"]) if cnt_row and cnt_row["cnt"] is not None else 10
							if cnt < 10:
								table_hint_lines.append(f"Allowed values for {text_col}: {samples}")
								table_hint_lines.append("Use exact match or ILIKE only with these values")
						except Exception:
							pass
				except Exception:
					# Best effort enrichment; skip sample extraction failures silently.
					pass

			# d) Completion hint based on child table name
			child_table_base = table.lower()
			if any(k in child_table_base for k in completion_table_keywords):
				for fk in fk_details:
					if fk["src_table"] != table_name:
						continue
					table_hint_lines.append(
						f"Completion hint: presence in {fk['src_table']} means the record in {fk['dst_table']} is complete"
					)

			# e) Reference columns hint (only if there is a FK)
			for col_meta in info.get("column_meta", []):
				col_name = col_meta["name"]
				if col_name.lower() not in reference_column_names:
					continue
				fk = fk_by_table_col.get((table_name, col_name))
				if not fk:
					continue
				table_hint_lines.append(
					f"Reference: {col_name} links to {fk['dst_table']} via FK -- use JOIN for human-readable names"
				)

			if table_hint_lines:
				for line in table_hint_lines:
					blueprint += f"{line}\n"
				auto_hints_lines.extend(table_hint_lines)

			blueprint += "\n"

		nullable_date_sql = """
		SELECT table_name, column_name, data_type
		FROM information_schema.columns
		WHERE table_schema = 'public'
		AND is_nullable = 'YES'
		AND data_type IN (
			'date', 'timestamp', 'timestamp without time zone',
			'timestamp with time zone', 'timestamptz'
		)
		AND table_name NOT IN (
			'pg_stat_statements', 'alembic_version'
		)
		ORDER BY table_name, column_name
		"""
		nullable_date_rows = await connection.fetch(nullable_date_sql)

		for r in nullable_date_rows:
			hint = f"Status hint: {r['table_name']}.{r['column_name']} IS NULL = pending/incomplete, IS NOT NULL = done/complete"
			auto_hints_lines.append(hint)

		pending_rule = "PENDING RULE: When user asks about pending, incomplete, or not done records — check the Status hints below first. Use IS NULL on the indicated column instead of filtering by a status value. Only use status column if schema sample values explicitly contain the word 'pending'."
		
		auto_hints = pending_rule + "\n" + "\n".join(auto_hints_lines).strip()
		return blueprint.strip(), auto_hints
	except Exception as e:
		raise ValueError(f"Failed to extract database blueprint: {_describe_connection_exception(e)}")
	finally:
		if connection is not None:
			await connection.close()

POSTGRES_SCHEMA_ANALYZER_SYSTEM_PROMPT = """
You are a Senior Database Architect and Business Analyst.
Your goal is to reverse engineer the business logic and semantic meaning of a PostgreSQL schema.

INPUT:
A raw technical PostgreSQL schema report with tables, columns, relationships, sample values,
status hints, boolean hints, and row-count estimates.

TASK:
Analyze the schema and output a detailed JSON object containing:
1. "business_summary": A high-level description of what this database is for.
2. "table_insights": A dictionary where keys are table names, containing:
   - "description": What this table represents.
   - "primary_keys": inferred primary keys.
   - "foreign_keys": inferred relationships.
   - "important_columns": columns that seem critical for analytics.
   - "column_descriptions": a dictionary mapping each column name to inferred meaning.
3. "suggested_semantic_schema": A concise text block documenting this database for a data assistant.

OUTPUT FORMAT:
Return ONLY valid JSON.
""".strip()

def _extract_postgres_tables_from_runtime_schema(runtime_schema: str) -> dict[str, list[str]]:
	tables: dict[str, list[str]] = {}
	for match in re.finditer(r"^Table `([^`]+)`[^\n]*\nColumns:\s*([^\n]+)", runtime_schema, flags=re.MULTILINE):
		table_name = match.group(1)
		columns_text = match.group(2)
		columns = re.findall(r"([a-zA-Z_][\w]*)\s*\(", columns_text)
		tables[table_name] = columns
	return tables

def _fallback_postgres_metadata(runtime_schema: str) -> dict[str, Any]:
	tables = _extract_postgres_tables_from_runtime_schema(runtime_schema)
	table_insights: dict[str, Any] = {}
	for table_name, columns in tables.items():
		primary_keys = [
			column
			for column in columns
			if column.lower() == "id" or column.lower().endswith("_id")
		][:2]
		important_columns = [
			column
			for column in columns
			if any(
				keyword in column.lower()
				for keyword in (
					"id", "name", "status", "date", "time", "amount", "total",
					"count", "email", "department", "assigned", "created", "updated",
				)
			)
		][:12]
		table_insights[table_name] = {
			"description": f"Inferred PostgreSQL table for `{table_name}` records.",
			"primary_keys": primary_keys,
			"foreign_keys": [],
			"important_columns": important_columns,
			"column_descriptions": {
				column: f"Inferred field from the `{table_name}` table."
				for column in columns
			},
		}

	table_names = ", ".join(f"'{table}'" for table in tables) or "no tables"
	return {
		"business_summary": f"This PostgreSQL database contains business data across {table_names}.",
		"table_insights": table_insights,
		"suggested_semantic_schema": (
			f"The database contains these logical tables: {table_names}. "
			"Use relationships, primary keys, important columns, and column descriptions to route user questions."
		),
	}

async def _analyze_postgres_schema(runtime_schema: str) -> dict[str, Any]:
	api_key = os.getenv("OPENAI_API_KEY", "").strip()
	if not api_key:
		logger.warning("OPENAI_API_KEY not configured; using deterministic PostgreSQL metadata fallback.")
		return _fallback_postgres_metadata(runtime_schema)

	try:
		from openai import AsyncOpenAI

		model_name = os.getenv("POSTGRES_SCHEMA_ANALYSIS_MODEL", os.getenv("SQL_GENERATION_MODEL", "gpt-5.2"))
		client = AsyncOpenAI(api_key=api_key)
		response = await client.chat.completions.create(
			model=model_name,
			temperature=0,
			response_format={"type": "json_object"},
			messages=[
				{"role": "system", "content": POSTGRES_SCHEMA_ANALYZER_SYSTEM_PROMPT},
				{"role": "user", "content": f"Here is the PostgreSQL schema report:\n\n{runtime_schema}"},
			],
		)
		content = response.choices[0].message.content or "{}"
		analysis = json.loads(content)
		if not isinstance(analysis, dict):
			raise ValueError("Schema analyzer returned non-object JSON.")
		return analysis
	except Exception as exc:
		logger.warning("PostgreSQL AI metadata analysis failed; using deterministic fallback: %s", exc)
		return _fallback_postgres_metadata(runtime_schema)

async def fetch_postgres_schema(connection_string: str) -> tuple[str, str]:
	"""Return metadata_analysis.json-style schema blueprint plus auto hints."""
	runtime_schema, auto_hints = await fetch_postgres_runtime_schema(connection_string)
	metadata_analysis = await _analyze_postgres_schema(runtime_schema)
	blueprint = json.dumps(metadata_analysis, indent=2, ensure_ascii=False)
	return blueprint, auto_hints

async def fetch_tenant_postgres_runtime_schema(tenant_id: uuid.UUID | str) -> tuple[str, str]:
	credential = await get_tenant_credentials(tenant_id)
	if credential is None:
		raise TenantDBConnectionError("Tenant database is not configured yet.")
	if credential.db_type.lower() != "postgresql":
		raise TenantDBConnectionError("Only PostgreSQL tenant databases are supported for PostgreSQL schema introspection.")

	connection_url = _decrypt_credential_value(credential.connection_url)
	return await fetch_postgres_runtime_schema(connection_url)

async def refresh_schema_blueprint(tenant_id: uuid.UUID | str) -> str:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))
	async with session_factory() as session:
		statement = (
			select(TenantDBCredential)
			.where(TenantDBCredential.tenant_id == tenant_uuid)
			.order_by(TenantDBCredential.last_connected_at.desc().nullslast())
			.limit(1)
		)
		result = await session.execute(statement)
		credential = result.scalars().first()
		if credential is None:
			raise ValueError("Tenant credentials not found.")
		credential_id = credential.id
		db_type = credential.db_type.lower()
		connection_url = _decrypt_credential_value(credential.connection_url)
		google_credentials = (
			_decrypt_credential_value(credential.google_credentials)
			if credential.google_credentials
			else None
		)

	if db_type == "postgresql":
		invalidate_runtime_schema_cache(credential_id)
		blueprint, auto_hints = await fetch_postgres_schema(connection_url)
	elif db_type == "google_sheets":
		if not google_credentials:
			raise ValueError("Google Sheets credentials are not configured.")
		sheet_id = connection_url.replace("google_sheets://", "")
		invalidate_sheets_data_cache(sheet_id)
		blueprint, auto_hints = await fetch_google_sheet_data(sheet_id, google_credentials)
	else:
		raise ValueError("Schema refresh is supported only for PostgreSQL and Google Sheets tenants.")

	async with session_factory() as session:
		credential = await session.get(TenantDBCredential, credential_id)
		if credential is None:
			raise ValueError("Tenant credentials not found.")
		credential.schema_blueprint = blueprint
		credential.auto_schema_hints = auto_hints
		await session.commit()

	# Drop any cached LLM-generated example questions for this credential —
	# the schema may now look quite different, so old examples could be wrong.
	# Then pre-warm the cache in the background so the user's next /start is
	# instant rather than waiting on an LLM round-trip.
	try:
		import asyncio as _asyncio
		from app.services.example_questions import (
			generate_example_questions,
			invalidate_example_cache,
		)
		invalidate_example_cache(credential_id)

		async def _prewarm_examples() -> None:
			try:
				# Look up the tenant's company name for the prompt.
				async with session_factory() as _s:
					cred = await _s.get(TenantDBCredential, credential_id)
					if cred is None:
						return
					tenant = await _s.get(Tenant, cred.tenant_id)
					company_name = tenant.company_name if tenant else "your"
				await generate_example_questions(
					company_name=company_name,
					schema_blueprint=blueprint,
					credential_id=credential_id,
					count=5,
				)
			except Exception as exc:
				logger.debug("[EXAMPLES] Pre-warm failed: %s", exc)

		_asyncio.create_task(_prewarm_examples())
	except Exception:
		# Cache invalidation / pre-warm must never fail the schema refresh itself.
		pass

	return blueprint

