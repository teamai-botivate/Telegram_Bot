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
def _infer_column_type(values: list[str]) -> str:
	"""Infer column type from non-empty cell values."""
	import re as _re
	non_empty = [v for v in values if v not in (None, "")]
	if not non_empty:
		return "text"
	bool_set = {"true", "false", "yes", "no", "1", "0"}
	if all(str(v).strip().lower() in bool_set for v in non_empty):
		return "boolean"
	if all(_re.fullmatch(r"-?\d+", str(v).strip()) for v in non_empty):
		return "integer"
	if all(_re.fullmatch(r"-?\d+\.?\d*", str(v).strip()) for v in non_empty):
		return "numeric"
	date_pat = _re.compile(
		r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
	)
	if all(date_pat.match(str(v).strip()) for v in non_empty):
		return "date"
	return "text"

def _compact_sheet_value(value: Any, max_length: int = 160) -> str:
	cleaned = str(value or "").replace("\n", " ").strip()
	if len(cleaned) <= max_length:
		return cleaned
	return cleaned[: max_length - 1].rstrip() + "…"

def _describe_sheet_from_headers(title: str, headers: list[str]) -> str:
	lowered_title = title.lower()
	lowered_headers = " ".join(headers).lower()
	text = f"{lowered_title} {lowered_headers}"

	if any(word in text for word in ("employee", "staff", "department", "designation", "manager", "salary")):
		return "Employee/HR records, useful for employee lookup, departments, managers, leave, and performance questions."
	if any(word in text for word in ("leave", "absence", "vacation")):
		return "Leave tracking records, useful for leave balance, leaves taken, upcoming leave, and leave reasons."
	if any(word in text for word in ("task", "pending", "deadline", "completion", "rating", "project")):
		return "Task and performance records, useful for pending work, completed tasks, deadlines, and ratings."
	if any(word in text for word in ("dashboard", "metric", "kpi", "summary")):
		return "Dashboard or KPI summary sheet, useful for high-level business metrics."
	return "General worksheet data. Use headers and row values to determine whether it answers the question."

def _important_sheet_columns(headers: list[str], col_types: dict[str, str]) -> list[str]:
	keywords = (
		"id",
		"name",
		"email",
		"phone",
		"department",
		"status",
		"manager",
		"date",
		"leave",
		"task",
		"pending",
		"deadline",
		"rating",
		"amount",
		"salary",
		"total",
		"count",
		"balance",
	)
	important = [
		header
		for header in headers
		if col_types.get(header) in {"integer", "numeric", "date", "boolean"}
		or any(keyword in header.lower() for keyword in keywords)
	]
	return important[:12]

GOOGLE_SHEETS_SKIP_TABS = {"readme", "instructions", "config"}

GOOGLE_SHEETS_ANALYZER_SYSTEM_PROMPT = """
You are a Senior Database Architect and Business Analyst.
Your goal is to reverse engineer the business logic and semantic meaning of a Google Sheets workbook schema.

INPUT:
A raw technical schema report with worksheet names, columns, inferred types, nullable flags,
categorical values, and a small sample of rows.

TASK:
Analyze the schema and output a detailed JSON object containing:
1. "business_summary": A high-level description of what this workbook/database is for.
2. "table_insights": A dictionary where keys are worksheet/table names, containing:
   - "description": What this worksheet represents.
   - "primary_keys": inferred primary keys.
   - "foreign_keys": inferred relationships or an empty list.
   - "important_columns": columns that seem critical for analytics.
   - "column_descriptions": a dictionary mapping each column name to inferred meaning.
3. "suggested_semantic_schema": A concise text block documenting this workbook for a data assistant.

OUTPUT FORMAT:
Return ONLY valid JSON.
""".strip()

def _load_google_spreadsheet(sheet_id: str, credentials_json: str) -> Any:
	import json
	import gspread
	from google.oauth2.service_account import Credentials

	scopes = [
		"https://www.googleapis.com/auth/spreadsheets.readonly",
		"https://www.googleapis.com/auth/drive.readonly",
	]

	creds_dict = json.loads(credentials_json)
	creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
	client = gspread.authorize(creds)
	return client.open_by_key(sheet_id)

def _collect_google_sheet_profiles(spreadsheet: Any) -> tuple[list[dict[str, Any]], str]:
	hint_lines: list[str] = []
	profiles: list[dict[str, Any]] = []

	for worksheet in spreadsheet.worksheets():
		title = worksheet.title
		if title.strip().lower() in GOOGLE_SHEETS_SKIP_TABS:
			continue

		all_values = worksheet.get_all_values()
		if not all_values:
			profiles.append(
				{
					"title": title,
					"row_count": 0,
					"headers": [],
					"col_types": {},
					"nullable": {},
					"description": "Empty worksheet.",
					"important_columns": [],
					"allowed_values": {},
					"sample_rows": [],
					"rows": [],
				}
			)
			continue

		headers = [h.strip() for h in all_values[0]]
		data_rows = all_values[1:]
		valid_indices = [i for i, h in enumerate(headers) if h]
		valid_headers = [headers[i] for i in valid_indices]
		if not valid_headers:
			continue

		col_values: dict[str, list[str]] = {}
		for idx, header in zip(valid_indices, valid_headers):
			col_values[header] = [
				row[idx].strip() if idx < len(row) else ""
				for row in data_rows
			]

		col_types = {header: _infer_column_type(col_values[header]) for header in valid_headers}
		nullable = {header: any(value == "" for value in col_values[header]) if data_rows else True for header in valid_headers}
		description = _describe_sheet_from_headers(title, valid_headers)
		important_columns = _important_sheet_columns(valid_headers, col_types)

		allowed_values: dict[str, list[str]] = {}
		for header in valid_headers:
			if col_types[header] not in ("text", "boolean"):
				continue
			distinct = sorted({value for value in col_values[header] if value})
			if 0 < len(distinct) <= 25:
				allowed_values[header] = distinct
				hint_lines.append(
					f"Allowed values for `{header}`: {distinct} - use exact match or case-insensitive contains"
				)

		hint_lines.append(f"Sheet `{title}`: {description}")
		if important_columns:
			hint_lines.append(f"Important columns in `{title}`: {important_columns}")

		status_keywords = {
			"completed", "done", "approved", "closed", "finished",
			"resolved", "verified", "paid", "delivered", "submission",
		}
		for header in valid_headers:
			if col_types[header] != "date":
				continue
			has_empty = any(value == "" for value in col_values[header])
			if has_empty and any(keyword in header.lower() for keyword in status_keywords):
				hint_lines.append(
					f"Status hint: Sheet `{title}` column `{header}` empty = pending/incomplete, "
					"IS NOT NULL = done/complete"
				)

		for header in valid_headers:
			if col_types[header] == "boolean":
				hint_lines.append(f"Boolean column: `{header}` - compare with TRUE/FALSE/Yes/No, never use ILIKE")

		rows: list[dict[str, Any]] = []
		for row_number, row in enumerate(data_rows, start=2):
			rows.append(
				{
					"row_number": row_number,
					"values": {
						header: _compact_sheet_value(row[index] if index < len(row) else "")
						for index, header in zip(valid_indices, valid_headers)
					},
				}
			)

		profiles.append(
			{
				"title": title,
				"row_count": len(data_rows),
				"headers": valid_headers,
				"col_types": col_types,
				"nullable": nullable,
				"description": description,
				"important_columns": important_columns,
				"allowed_values": allowed_values,
				"sample_rows": rows[:3],
				"rows": rows,
			}
		)

	pending_rule = (
		"PENDING RULE: When the user asks about pending, incomplete, or not done records - "
		"check Status hints first. Use empty/blank check on the indicated column instead of "
		"filtering by a text value. Only filter by text if the Allowed values list explicitly "
		"contains the word 'pending'."
	)
	return profiles, pending_rule + "\n" + "\n".join(hint_lines)

def _normalize_sheet_match_text(value: Any) -> str:
	return re.sub(r"\s+", " ", str(value or "").strip()).lower()

def _question_contains_sheet_value(question_norm: str, value_norm: str) -> bool:
	if not value_norm:
		return False
	if len(value_norm) <= 3 or re.fullmatch(r"[\w.-]+", value_norm):
		return bool(re.search(rf"(?<!\w){re.escape(value_norm)}(?!\w)", question_norm))
	return value_norm in question_norm

def _is_sheet_match_candidate(value: Any) -> bool:
	text = str(value or "").strip()
	if len(text) < 2 or len(text) > 80:
		return False
	if text.lower() in {"yes", "no", "true", "false", "n/a", "na", "none", "-"}:
		return False
	return bool(re.search(r"[A-Za-z0-9]", text))

def _extract_question_sheet_values(profiles: list[dict[str, Any]], question: str) -> list[str]:
	question_norm = _normalize_sheet_match_text(question)
	matched: list[str] = []
	seen: set[str] = set()

	for profile in profiles:
		for row in profile.get("rows", []):
			values = row.get("values", {}) if isinstance(row, dict) else {}
			for value in values.values():
				if not _is_sheet_match_candidate(value):
					continue
				value_text = str(value).strip()
				value_norm = _normalize_sheet_match_text(value_text)
				if value_norm in seen:
					continue
				if _question_contains_sheet_value(question_norm, value_norm):
					seen.add(value_norm)
					matched.append(value_text)

	return sorted(matched, key=len, reverse=True)

def _build_google_sheet_targeted_match_context(
	profiles: list[dict[str, Any]],
	question: str | None,
	max_rows_per_sheet: int = 20,
) -> str:
	"""Return exact-value row matches computed from all loaded sheet rows.

	This is schema-agnostic: it looks for concrete cell values mentioned in the
	user question, then counts rows per worksheet containing all those values.
	"""
	if not question:
		return ""

	matched_values = _extract_question_sheet_values(profiles, question)
	if not matched_values:
		return ""

	matched_norms = [_normalize_sheet_match_text(value) for value in matched_values]
	lines: list[str] = [
		"TARGETED ROW MATCHES FOR CURRENT QUESTION (computed from all worksheet rows before snapshot truncation):",
		f"Matched cell values from question: {matched_values}",
	]

	for profile in profiles:
		sheet_matches: list[dict[str, Any]] = []
		for row in profile.get("rows", []):
			values = row.get("values", {}) if isinstance(row, dict) else {}
			row_blob = "\n".join(_normalize_sheet_match_text(value) for value in values.values())
			if all(value_norm in row_blob for value_norm in matched_norms):
				sheet_matches.append(row)

		title = profile.get("title", "Untitled")
		lines.append(f"Sheet `{title}`: {len(sheet_matches)} rows contain all matched cell values.")
		for row in sheet_matches[:max_rows_per_sheet]:
			lines.append(f"  Row {row.get('row_number', '?')}: {row.get('values', {})}")
		if len(sheet_matches) > max_rows_per_sheet:
			lines.append(f"  {len(sheet_matches) - max_rows_per_sheet} additional matching rows omitted.")

	return "\n".join(lines)

def _google_sheet_schema_report(spreadsheet_title: str, profiles: list[dict[str, Any]]) -> str:
	lines = [
		f"# Schema Report: {spreadsheet_title}",
		"",
		"---",
		"",
	]

	for profile in profiles:
		title = profile["title"]
		lines.append(f"## Table: `{title}`")
		lines.append(f"Description: {profile['description']}")
		lines.append(f"Rows: ~{profile['row_count']}")
		lines.append("")
		lines.append("### Columns")
		lines.append("| Name | Type | Nullable |")
		lines.append("| :--- | :--- | :--- |")
		for header in profile["headers"]:
			lines.append(
				f"| **{header}** | `{profile['col_types'][header]}` | {profile['nullable'][header]} |"
			)
		lines.append("")
		lines.append("### Categorical / Allowed Values")
		if profile["allowed_values"]:
			for header, values in profile["allowed_values"].items():
				lines.append(f"- **`{header}`** ({len(values)} values): `{values}`")
		else:
			lines.append("_No categorical columns detected_")
		lines.append("")
		lines.append("### Sample Data")
		if profile["sample_rows"]:
			headers = profile["headers"]
			lines.append("| " + " | ".join(headers) + " |")
			lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
			for row in profile["sample_rows"]:
				values = [
					_compact_sheet_value(row["values"].get(header, ""), max_length=80).replace("|", "/")
					for header in headers
				]
				lines.append("| " + " | ".join(values) + " |")
		else:
			lines.append("_No data_")
		lines.append("")
		lines.append("---")
		lines.append("")

	return "\n".join(lines).strip()

async def fetch_google_sheet_data(sheet_id: str, credentials_json: str) -> tuple[str, str]:
	"""Return metadata_analysis.json-style schema blueprint plus auto hints.

	The stored schema_blueprint must stay semantic metadata only. Live row data is
	fetched separately at message time by fetch_google_sheet_runtime_context().

	gspread is synchronous and will block the event loop, so the fetch + profile
	stages are run in a thread pool. The OpenAI call uses AsyncOpenAI directly.
	"""
	def _gspread_blocking() -> tuple[str, str, list[dict[str, Any]], str]:
		spreadsheet = _load_google_spreadsheet(sheet_id, credentials_json)
		profiles, hints = _collect_google_sheet_profiles(spreadsheet)
		schema_report = _google_sheet_schema_report(spreadsheet.title, profiles)
		return spreadsheet.title, schema_report, profiles, hints

	# asyncio.to_thread keeps the event loop responsive (health checks, other requests)
	# while gspread does its blocking I/O.
	spreadsheet_title, schema_report, profiles, hints = await asyncio.to_thread(_gspread_blocking)
	metadata_analysis = await _analyze_google_sheet_schema(spreadsheet_title, schema_report, profiles)
	blueprint = json.dumps(metadata_analysis, indent=2, ensure_ascii=False)
	return blueprint, hints

async def fetch_google_sheet_runtime_context(
	sheet_id: str,
	credentials_json: str,
	question: str | None = None,
) -> tuple[str, str]:
	"""Return live Google Sheets rows for answering. This output is not stored.

	The expensive gspread I/O (spreadsheet load + profile collection) is cached
	per sheet_id for SHEETS_CACHE_TTL_SECONDS (default 60s). Question-specific
	context (targeted matches, row formatting) is rebuilt each time.

	gspread is synchronous, so the fetch is offloaded to a thread pool.
	"""
	now = time.monotonic()
	cached = _sheets_data_cache.get(sheet_id)

	if cached is not None:
		ts, cached_profiles, cached_hints, cached_title = cached
		if now - ts < SHEETS_CACHE_TTL_SECONDS:
			logger.debug("[SHEETS_CACHE] HIT sheet=%s age=%.1fs", sheet_id, now - ts)
			profiles, hints, spreadsheet_title = cached_profiles, cached_hints, cached_title
		else:
			cached = None  # expired

	if cached is None:
		logger.info("[SHEETS_CACHE] MISS sheet=%s — fetching from Google API", sheet_id)

		def _blocking_fetch() -> tuple[list[dict[str, Any]], str, str]:
			spreadsheet = _load_google_spreadsheet(sheet_id, credentials_json)
			profs, hnts = _collect_google_sheet_profiles(spreadsheet)
			return profs, hnts, spreadsheet.title

		profiles, hints, spreadsheet_title = await asyncio.to_thread(_blocking_fetch)
		_sheets_data_cache[sheet_id] = (now, profiles, hints, spreadsheet_title)

	# Question-specific context is always rebuilt from cached/fresh profiles
	lines: list[str] = [f"Google Sheets Live Data Context: {spreadsheet_title}", ""]
	targeted_matches = _build_google_sheet_targeted_match_context(profiles, question)

	if targeted_matches:
		lines.append(targeted_matches)
		lines.append("")
		# Targeted matches found — no need to dump full rows. Just provide headers for context.
		for profile in profiles:
			lines.append(f"Sheet `{profile['title']}` | Rows: ~{profile['row_count']}")
			lines.append(f"Description: {profile['description']}")
			lines.append(f"Columns: {', '.join(profile['headers'])}\n")
	else:
		# No exact matches found. Fallback to a small snapshot.
		fallback_row_limit = int(os.getenv("GOOGLE_SHEETS_FALLBACK_ROW_LIMIT", "50"))
		for profile in profiles:
			lines.append(f"Sheet `{profile['title']}` | Rows: ~{profile['row_count']}")
			lines.append(f"Description: {profile['description']}")
			lines.append(f"Columns: {', '.join(profile['headers'])}")
			visible_rows = profile["rows"][:fallback_row_limit]
			lines.append(f"Full data snapshot ({len(visible_rows)} of {profile['row_count']} rows):")
			for row in visible_rows:
				lines.append(f"  Row {row['row_number']}: {row['values']}")
			if profile["row_count"] > len(visible_rows):
				lines.append(f"  Snapshot truncated: {profile['row_count'] - len(visible_rows)} additional rows are not included.")
			lines.append("")

	return "\n".join(lines).strip(), hints

def invalidate_sheets_data_cache(sheet_id: str | None = None) -> None:
	"""Clear cached Google Sheets data. Pass a sheet_id to clear one entry, or None to clear all."""
	if sheet_id is not None:
		if _sheets_data_cache.pop(sheet_id, None) is not None:
			logger.info("[SHEETS_CACHE] Invalidated sheet=%s", sheet_id)
	else:
		_sheets_data_cache.clear()
		logger.info("[SHEETS_CACHE] Invalidated ALL entries")

