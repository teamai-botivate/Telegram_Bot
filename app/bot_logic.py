from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
try:
    from openai import AsyncOpenAI
except ImportError as _openai_error:  # pragma: no cover - exercised in environments without openai installed
    AsyncOpenAI = Any  # type: ignore[assignment]
else:
    _openai_error = None

from .database import (
    QueryExecutionError,
    SecurityError,
    TenantDBConnectionError,
    execute_tenant_query,
    explain_validate_sql,
    fetch_tenant_postgres_runtime_schema,
    get_tenant_by_chat_id,
    get_tenant_credentials,
    retrieve_similar_examples,
    store_query_example,
)
from .platforms.base import BotMessage, Platform, send_reply

load_dotenv()

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_CHAT_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_CLASSIFIER_MODEL = os.getenv("MISTRAL_CLASSIFIER_MODEL", "mistral-large-2512")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SQL_GENERATION_MODEL = os.getenv("SQL_GENERATION_MODEL", "gpt-5.2")
RESPONSE_FORMAT_MODEL = os.getenv("RESPONSE_FORMAT_MODEL", "gpt-5.2")
ENABLE_QUERY_LEARNING = os.getenv("ENABLE_QUERY_LEARNING", "true").strip().lower() == "true"
ACCOUNT_NOT_FOUND_MESSAGE = "Hi! I couldn't find your account. Please contact support."
GENERIC_FAILURE_MESSAGE = "Sorry, I ran into an issue while processing your request. Please try again."
RETRIEVAL_FAILURE_MESSAGE = "I wasn't able to retrieve that information right now. Please try rephrasing your question."
DATABASE_CONNECTION_MESSAGE = (
    "I'm having trouble connecting to your database right now. "
    "Please contact Botivate support if this persists."
)

_openai_client: AsyncOpenAI | None = None
_conversation_context: dict[str, list[dict[str, Any]]] = {}
MAX_CONVERSATION_CONTEXT_ITEMS = 3


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_error is not None:
        raise RuntimeError("openai package is not installed. Add it to environment with pip install -r requirements.txt.")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured in .env.")
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def _extract_assistant_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    return ""


async def _call_mistral(messages: list[dict[str, str]], max_tokens: int, model: str) -> str:
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY is not configured in .env.")

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(MISTRAL_CHAT_COMPLETIONS_URL, headers=headers, json=payload)

    response.raise_for_status()
    response_data = response.json()
    assistant_text = _extract_assistant_text(response_data)
    return assistant_text or ""


async def _call_openai_sql(system_prompt: str, user_prompt: str) -> str:
    client = _get_openai_client()
    completion = await client.chat.completions.create(
        model=SQL_GENERATION_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    return (content or "").strip()


async def _call_openai_formatting(system_prompt: str, user_prompt: str, max_tokens: int = 600) -> str:
    client = _get_openai_client()
    completion = await client.chat.completions.create(
        model=RESPONSE_FORMAT_MODEL,
        temperature=0,
        max_completion_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    return (content or "").strip()


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql|json)?", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return cleaned


def _context_key(msg: BotMessage) -> str:
    return f"{msg.platform.value}:{msg.chat_id}"


def _build_conversation_context_block(msg: BotMessage) -> str:
    history = _conversation_context.get(_context_key(msg), [])
    if not history:
        return ""

    lines = ["RECENT CHAT CONTEXT (use only when the current question is a follow-up):"]
    for index, item in enumerate(history[-MAX_CONVERSATION_CONTEXT_ITEMS:], start=1):
        lines.append(f"{index}. User: {item.get('question', '')}")
        if item.get("sql"):
            lines.append(f"   SQL: {item['sql']}")
        if item.get("reply"):
            lines.append(f"   Assistant: {item['reply']}")
    lines.append(
        "If the current question is short or elliptical, inherit relevant table/filter/status constraints from this context. "
        "If the current question clearly changes scope, follow the current question."
    )
    return "\n".join(lines)


def _remember_conversation_context(
    msg: BotMessage,
    question: str,
    reply: str,
    sql: str | None = None,
) -> None:
    key = _context_key(msg)
    history = _conversation_context.setdefault(key, [])
    history.append(
        {
            "question": question,
            "reply": reply,
            "sql": sql,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    del history[:-MAX_CONVERSATION_CONTEXT_ITEMS]


def _extract_entities(question: str) -> dict[str, Any]:
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", question)
    numbers = re.findall(r"\b\d+(?:\.\d+)?\b", question)
    quoted_terms = re.findall(r"['\"]([^'\"]+)['\"]", question)

    return {
        "dates": dates,
        "emails": emails,
        "numbers": numbers,
        "quoted_terms": quoted_terms,
        "today_utc": datetime.now(timezone.utc).date().isoformat(),
    }


def _extract_name_filters(question: str, auto_schema_hints: str | None) -> str:
    """Match person names from the question against the tenant's actual data values.

    Parses 'Allowed values for <column>: [values]' lines from auto_schema_hints,
    checks which values appear in the question, and returns ready-to-use
    filter instructions the LLM cannot ignore.

    Works with ANY tenant database — reads from their own hints.
    """
    if not auto_schema_hints:
        return ""

    # Parse "Allowed values for <column>: ['val1', 'val2']" lines.
    # Google Sheets headers often contain spaces and are wrapped in backticks,
    # while Postgres columns are usually simple identifiers.
    name_columns = {}  # {column_name: [values]}
    for line in auto_schema_hints.split("\n"):
        match = re.match(
            r"Allowed values for `?([^`:]+?)`?:\s*\[(.+?)\](?:\s|$)",
            line.strip(),
        )
        if not match:
            continue
        col_name = match.group(1).lower()
        # Only check name-like columns
        if not any(kw in col_name for kw in ("name", "given_by", "assigned", "user_name", "employee", "worker")):
            continue
        # Parse the values list
        raw_values = match.group(2)
        values = [v.strip().strip("'\"") for v in raw_values.split(",")]
        values = [v for v in values if v and len(v) > 1]
        name_columns[match.group(1)] = values

    if not name_columns:
        return ""

    # Match question against known values (case-insensitive, longest first)
    question_lower = question.lower()
    found_filters: list[str] = []

    for col_name, values in name_columns.items():
        # Sort by length descending so "Am Sir" matches before "Am"
        for val in sorted(values, key=len, reverse=True):
            if val.lower() in question_lower:
                found_filters.append(f"{col_name} ILIKE '%{val}%'")

    if not found_filters:
        return ""

    # Deduplicate
    seen = set()
    unique_filters = []
    for f in found_filters:
        if f.lower() not in seen:
            seen.add(f.lower())
            unique_filters.append(f)

    filter_text = " OR ".join(unique_filters)
    return (
        f"\nPERSON FILTER (MANDATORY — the user mentioned specific people):\n"
        f"You MUST include this WHERE condition: {filter_text}\n"
        f"Do NOT omit this filter. The user is asking about specific people.\n"
    )


def _extract_sheet_value_filters(question: str, sheet_hints: str | None) -> str:
    """Build natural-language filter instructions from allowed Sheet values.

    The Google Sheets flow does not generate SQL, so the prompt needs plain
    instructions rather than SQL WHERE fragments. This also supports headers
    such as `Employee Name`, which cannot be parsed by the SQL-oriented helper.
    """
    if not sheet_hints:
        return ""

    question_lower = question.lower()
    explicit_employee_name_lookup = bool(
        re.search(
            r"\b(employee|person|worker|staff|user)\s+(?:named|called|name(?:d)?\s+is)\b",
            question_lower,
        )
        or re.search(r"\bnamed\s+['\"]?[^'\"]+", question_lower)
    )
    matched_filters: list[tuple[str, str]] = []

    def _value_mentioned(value: str) -> bool:
        value_lower = value.lower()
        if len(value_lower) <= 3:
            return bool(re.search(rf"(?<!\w){re.escape(value_lower)}(?!\w)", question_lower))
        return value_lower in question_lower

    for line in sheet_hints.splitlines():
        match = re.match(
            r"Allowed values for `?([^`:]+?)`?:\s*\[(.+?)\](?:\s|$)",
            line.strip(),
        )
        if not match:
            continue

        column_name = match.group(1).strip()
        raw_values = match.group(2)
        values = [value.strip().strip("'\"") for value in raw_values.split(",")]
        mentioned_values = [value for value in values if len(value) > 1 and _value_mentioned(value)]

        lowered_column = column_name.lower()
        is_employee_name_column = (
            "employee name" in lowered_column
            or lowered_column in {"name", "full name", "person name", "staff name", "worker name", "user name"}
        )
        is_manager_column = "manager" in lowered_column
        is_department_column = "department" in lowered_column
        is_status_column = "status" in lowered_column
        is_assignment_column = any(keyword in lowered_column for keyword in ("assigned", "given"))

        if explicit_employee_name_lookup and not is_employee_name_column:
            continue

        if is_manager_column and "manager" not in question_lower and not re.search(r"\breports?\s+to\b", question_lower):
            continue

        if is_department_column and "department" not in question_lower and not mentioned_values:
            continue

        if is_status_column and "status" not in question_lower and not mentioned_values:
            continue

        if is_assignment_column and not any(keyword in question_lower for keyword in ("assigned", "given by", "given to")):
            continue

        if not any((is_employee_name_column, is_manager_column, is_department_column, is_status_column, is_assignment_column)):
            continue

        for value in sorted(mentioned_values, key=len, reverse=True):
            matched_filters.append((column_name, value))
            break

    if not matched_filters:
        return ""

    deduped: list[tuple[str, str]] = []
    seen = set()
    for column_name, value in matched_filters:
        key = (column_name.lower(), value.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append((column_name, value))

    lines = "\n".join(
        f"- Match `{column_name}` to `{value}` case-insensitively."
        for column_name, value in deduped
    )
    return (
        "\nMATCHED FILTERS FROM USER QUESTION (mandatory):\n"
        f"{lines}\n"
        "Apply these filters before counting, listing, or summarizing rows.\n"
    )


def _asks_for_everything(question: str) -> bool:
    return bool(re.search(r"\b(all|everything|entire|whole|complete)\b", question, flags=re.IGNORECASE))


async def is_off_topic(text: str) -> bool:
    prompt = (
        "You are a classifier for a business database chatbot.\n"
        "The user can ask about ANY data that might exist in their company database.\n"
        "This includes but is not limited to:\n"
        "- employees, users, staff, team members\n"
        "- emails, phone numbers, addresses, passwords, credentials\n"
        "- tasks, checklists, assignments, deadlines\n"
        "- meetings, schedules, attendance, leaves\n"
        "- deliveries, orders, invoices, payments\n"
        "- departments, roles, access levels\n"
        "- reports, counts, summaries, statistics\n"
        "- any data lookup by name, date, status, or ID\n\n"
        "Reply YES if the message is asking about data that could exist in a business database.\n"
        "Reply NO only if the message is clearly personal chat, jokes, general knowledge, "
        "weather, news, or completely unrelated to any business data.\n\n"
        f"Message: {text}\n"
        "Answer (YES or NO):"
    )
    try:
        answer = await _call_mistral(
            [{"role": "user", "content": prompt}],
            max_tokens=5,
            model=MISTRAL_CLASSIFIER_MODEL,
        )
    except Exception as exc:
        logger.warning("Off-topic detection failed open: %s", exc)
        return False

    # Note: YES means it IS business-related, so off-topic = starts with NO
    return answer.strip().upper().startswith("NO")


def detect_multi_table_query(text: str) -> bool:
    patterns = (
        "each table",
        "all tables",
        "every table",
        "from all",
        "list tables",
        "show tables",
    )
    lowered = text.lower()
    return any(p in lowered for p in patterns)


def _extract_table_names_from_blueprint(schema_blueprint: str) -> list[str]:
    return re.findall(r"^Table `([^`]+)`", schema_blueprint, flags=re.MULTILINE)


def build_table_aliases(schema_blueprint: str) -> str:
    table_names = _extract_table_names_from_blueprint(schema_blueprint)
    if not table_names:
        return "No tables found."

    aliases = {}
    used = set()
    for table in table_names:
        for length in range(1, len(table) + 1):
            candidate = table[:length].lower()
            if candidate not in used:
                aliases[table] = candidate
                used.add(candidate)
                break

    return ", ".join(f"{t} -> {a}" for t, a in aliases.items())


def _extract_columns_for_table(schema_blueprint: str, table_name: str) -> list[str]:
    """
    Extract column names for a specific table from the schema blueprint string.
    """
    pattern = rf"Table `{re.escape(table_name)}`.*?Columns:\s*([^\n]+)"
    match = re.search(pattern, schema_blueprint, re.DOTALL)
    if not match:
        return []

    columns_str = match.group(1)
    # Parse "col_name (type), col_name2 (type)" format.
    return re.findall(r"(\w+)\s*\(", columns_str)


def build_dynamic_examples(schema_blueprint: str) -> str:
    """
    Build schema-based prompt examples using actual table and column names.
    """
    tables = _extract_table_names_from_blueprint(schema_blueprint)
    if not tables:
        return "No schema-derived examples available."

    examples: list[str] = []

    # Build UNION ALL example from first two detected tables.
    if len(tables) >= 2:
        t1, t2 = tables[0], tables[1]
        t1_cols = _extract_columns_for_table(schema_blueprint, t1)
        t2_cols = _extract_columns_for_table(schema_blueprint, t2)
        if t1_cols and t2_cols:
            t1_alias = t1[0].lower()
            t2_alias = t2[0].lower()
            shared_count = min(3, len(t1_cols), len(t2_cols))
            t1_select = ", ".join([f"{t1_alias}.{c}::text" for c in t1_cols[:shared_count]])
            t2_select = ", ".join([f"{t2_alias}.{c}::text" for c in t2_cols[:shared_count]])
            examples.append(
                f"""UNION ALL example using your schema:
SELECT {t1_select}, '{t1}' AS table_source
FROM {t1} AS {t1_alias}
UNION ALL
SELECT {t2_select}, '{t2}' AS table_source
FROM {t2} AS {t2_alias}"""
            )

    # Build alias declaration example from first detected table.
    first_table = tables[0]
    first_cols = _extract_columns_for_table(schema_blueprint, first_table)
    if first_cols:
        alias = first_table[0].lower()
        select_cols = ", ".join([f"{alias}.{c}" for c in first_cols[:4]])
        examples.append(
            f"""Alias declaration example using your schema:
SELECT {select_cols}
FROM {first_table} AS {alias}
WHERE {alias}.{first_cols[0]} IS NOT NULL"""
        )

    return "\n\n".join(examples) if examples else "No schema-derived examples available."


def _question_mentions_schema_table(question: str, schema_blueprint: str) -> bool:
    lowered_question = question.lower()
    for table_name in _extract_table_names_from_blueprint(schema_blueprint):
        lowered_table = table_name.lower()
        if lowered_table in lowered_question:
            return True

        spaced_variant = lowered_table.replace("_", " ")
        if spaced_variant in lowered_question:
            return True

    return False


def _extract_tables_with_column(schema_blueprint: str, column_name: str) -> list[str]:
    target = column_name.lower()
    matches: list[str] = []

    for table_name in _extract_table_names_from_blueprint(schema_blueprint):
        columns = [column.lower() for column in _extract_columns_for_table(schema_blueprint, table_name)]
        if target in columns:
            matches.append(table_name)

    return matches


def _maybe_expand_count_query_across_tables(sql: str, schema_blueprint: str, question: str) -> str:
    lowered_sql = sql.lower()
    if "count(" not in lowered_sql or "union all" in lowered_sql or " join " in lowered_sql:
        return sql

    if not re.search(r"\bhow\s+many\b|\bcount\b", question, flags=re.IGNORECASE):
        return sql

    if _question_mentions_schema_table(question, schema_blueprint):
        return sql

    from_match = re.search(r"\bfrom\s+([a-zA-Z_][\w]*)\b", sql, flags=re.IGNORECASE)
    where_match = re.search(
        r"\bwhere\s+(?:[a-zA-Z_][\w]*\.)?([a-zA-Z_][\w]*)\s+ilike\s+('(?:''|[^'])*')",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not from_match or not where_match:
        return sql

    source_table = from_match.group(1)
    filter_column = where_match.group(1)
    filter_literal = where_match.group(2)

    candidate_tables = _extract_tables_with_column(schema_blueprint, filter_column)
    if len(candidate_tables) <= 1:
        return sql

    if source_table not in candidate_tables:
        candidate_tables.insert(0, source_table)

    subqueries: list[str] = []
    for index, table_name in enumerate(candidate_tables, start=1):
        alias = f"t{index}"
        subqueries.append(
            f"SELECT 1 AS row_marker FROM {table_name} AS {alias} "
            f"WHERE {alias}.{filter_column} ILIKE {filter_literal}"
        )

    return (
        "SELECT COUNT(*) AS total_count\n"
        "FROM (\n"
        + "\nUNION ALL\n".join(subqueries)
        + "\n) AS combined_rows"
    )


async def _plan_query(
    schema_blueprint: str,
    question: str,
    auto_schema_hints: str | None = None,
    similar_questions_block: str = "",
    conversation_context_block: str = "",
) -> str:
    """Step 1 of Chain-of-Thought: Analyze the schema and produce a structured
    query plan WITHOUT writing any SQL. Works with any tenant schema."""
    if auto_schema_hints and auto_schema_hints.strip():
        hints_section = auto_schema_hints.strip()
    else:
        hints_section = "No auto-inferred schema rules available."

    # Pre-compute person name filters from the tenant's own data values
    person_filter = _extract_name_filters(question, auto_schema_hints)

    plan_prompt = f"""Analyze the question and schema. Output a QUERY PLAN only — no SQL.

SCHEMA:
{schema_blueprint}
{similar_questions_block}
{conversation_context_block}
RULES:
{hints_section}
{person_filter}
QUESTION: {question}

PLANNING INSTRUCTIONS:
1. DISTINCT: If the question asks "how many people/employees/workers"
   or "list/name the people" — use COUNT(DISTINCT column) or
   SELECT DISTINCT. A table may have many rows per person.
2. Only use tables and columns that EXIST in the schema above.
3. PENDING/STATUS: Look at the schema sample values.
   - If a column has values like 'pending','completed','not started'
     → filter by that text column
   - If schema rules say "column IS NULL = pending"
     → use IS NULL on that date column
4. If a PERSON FILTER section appears above, you MUST include
   those exact WHERE conditions in your FILTERS output.
5. For boolean columns → TRUE/FALSE, never text
6. Ignore tables starting with "extensions." or "pg_"
7. MULTI-TABLE: If the question asks about multiple tables,
   query each table separately with UNION ALL.
8. FOLLOW-UP QUESTIONS: If RECENT CHAT CONTEXT shows the user was asking
   about pending/status/table filters and the current question is short
   (for example "Task in delegation?"), preserve those constraints unless
   the current question clearly changes them.
9. COUNT + WHO/BY WHOM: If the user asks for a count and also asks who
   gave/assigned/owns those records, GROUP BY the giver/assignee/owner
   column and return one row per group. Do not collapse multiple people
   into one arbitrary value.

OUTPUT FORMAT:
TABLES: [tables to query]
COLUMNS: [columns to SELECT — use DISTINCT if asking for unique values]
FILTERS: [WHERE conditions — MUST include PERSON FILTER if present above]
JOINS: [JOIN conditions, or "none"]
AGGREGATION: [COUNT/SUM/AVG, use DISTINCT if counting unique, or "none"]
ORDER: [ORDER BY, or "none"]
LIMIT: [number or 50]
REASONING: [1 sentence: why these tables and columns]""".strip()

    plan = await _call_openai_sql(plan_prompt, f"Create a query plan for: {question}")
    logger.info("[SQL_PLAN] %s", plan.replace("\n", " | "))
    return plan


async def generate_sql_query(
    company_name: str,
    schema_blueprint: str,
    question: str,
    auto_schema_hints: str | None = None,
    tenant_id: Any = None,
    product_connection_id: Any = None,
    conversation_context_block: str = "",
) -> str:
    """Two-step Chain-of-Thought SQL generation.

    Step 1: Plan which tables, columns, filters, and joins to use.
    Step 2: Convert the plan into a valid PostgreSQL SELECT query.

    Fully multi-tenant — uses the tenant's own schema_blueprint and hints.
    """
    # ── Few-shot retrieval (best-effort; never raises) ──
    examples: list[dict[str, Any]] = []
    if tenant_id is not None:
        try:
            examples = await retrieve_similar_examples(
                tenant_id, question, product_connection_id=product_connection_id, limit=5
            )
        except Exception as retrieval_error:
            logger.warning("[FEW_SHOT] retrieval failed: %s", retrieval_error)
            examples = []

    top_sim = examples[0]["similarity"] if examples else 0
    logger.info(f"[FEW_SHOT] retrieved={len(examples)} top_sim={top_sim:.3f}")

    if examples:
        few_shot_block = "EXAMPLES of past successful queries on this database (for reference, not copying):\n"
        for ex in examples:
            few_shot_block += f"Q: {ex['question']}\nSQL: {ex['sql']}\n\n"
        few_shot_block = few_shot_block.rstrip() + "\n\nThese examples show patterns that worked before. Adapt them to the current question — do not copy verbatim if the question differs.\n"
        similar_questions_block = "\nSIMILAR PAST QUESTIONS (for context):\n" + "\n".join(
            f"- {ex['question']}" for ex in examples
        ) + "\n"
    else:
        few_shot_block = ""
        similar_questions_block = ""

    # ── Step 1: Generate a structured query plan ──
    plan = await _plan_query(
        schema_blueprint,
        question,
        auto_schema_hints,
        similar_questions_block=similar_questions_block,
        conversation_context_block=conversation_context_block,
    )

    # ── Step 2: Convert plan to SQL ──
    entities = _extract_entities(question)
    entities_json = json.dumps(entities, default=str)
    dynamic_aliases = build_table_aliases(schema_blueprint)
    person_filter = _extract_name_filters(question, auto_schema_hints)

    if auto_schema_hints and auto_schema_hints.strip():
        hints_section = auto_schema_hints.strip()
    else:
        hints_section = "No auto-inferred schema rules available."

    system_prompt = f"""Convert this QUERY PLAN into a PostgreSQL SELECT query.
Output ONLY raw SQL. No markdown, no backticks, no explanation, no semicolon.

PLAN:
{plan}

SCHEMA:
{schema_blueprint}

{few_shot_block}TABLE ALIASES: {dynamic_aliases}
{conversation_context_block}

RULES:
{hints_section}
{person_filter}
SQL REQUIREMENTS:
- If the plan says DISTINCT, you MUST use DISTINCT in the SQL
- If a PERSON FILTER section appears above, you MUST include
  those exact WHERE conditions in the SQL
- Declare aliases: FROM table AS alias
- Never use SELECT * — list columns explicitly
- ILIKE for text searches, = TRUE/FALSE for booleans
- IS NULL / IS NOT NULL for nullable dates
- LEFT JOIN preferred over INNER JOIN
- Only use columns that exist in the schema
- LIMIT must be included
- For UNION ALL, cast columns to ::text
- PostgreSQL does not support COUNT(DISTINCT ...) OVER (). If you need
  distinct rows plus a total count, use SELECT DISTINCT in a subquery and
  COUNT(*) OVER () in the outer query.
- If the question asks for separate counts per table/category/person, return
  those as separate named columns or rows with clear labels.
- If the question asks "who gave/assigned/owns" along with a count, GROUP BY
  the giver/assignee/owner column and include that column in SELECT.
- For short follow-up questions, preserve relevant table/filter/status
  constraints from RECENT CHAT CONTEXT unless the user clearly changes scope.

QUESTION: {question}
ENTITIES: {entities_json}""".strip()

    user_prompt = f"Generate the SQL query for: {question}"
    raw_sql = await _call_openai_sql(system_prompt, user_prompt)
    return _strip_code_fences(raw_sql)


async def fix_sql(sql: str, error: str, schema_blueprint: str) -> str:
    system_prompt = f"""
Fix this PostgreSQL query that produced an error.
Return ONLY corrected raw SQL query. No markdown. No backticks. No explanation.
Only SELECT statements are allowed.

SCHEMA:
{schema_blueprint}

BROKEN SQL:
{sql}

ERROR:
{error}

COMMON FIXES:
- UNION ALL type mismatch: cast all columns to ::text
- Alias not found: declare alias in FROM/JOIN clause first
- Column not found: check exact column name in schema above
- Boolean column: use = TRUE or = FALSE, never ILIKE
- Nullable column: use IS NULL or IS NOT NULL
- DISTINCT window count: PostgreSQL does not support COUNT(DISTINCT ...) OVER ().
  Put SELECT DISTINCT in a subquery, then use COUNT(*) OVER () in the outer query.

Corrected SQL:
""".strip()
    user_prompt = "Fix the SQL query."

    fixed_sql = await _call_openai_sql(system_prompt, user_prompt)
    return _strip_code_fences(fixed_sql)


async def format_sql_response(company_name: str, question: str, sql_results: list[dict[str, Any]]) -> str:
    total_rows = len(sql_results)
    display_rows = sql_results[:10]

    system_prompt = f"""Reply in English only. Plain text only — no markdown,
no **bold**, no | tables |. Keep it short (3-8 lines max).

You are {company_name}'s data assistant. The user asked: "{question}"
{total_rows} rows returned. Data (first {len(display_rows)}):
{json.dumps(display_rows, default=str)}

HOW TO RESPOND:
- Single count → state it: "There are 835 pending tasks."
- Multiple counts → list each: "HR: 45, IT: 23, Production: 12"
- List of names → list them comma-separated
- Record details → show 2-3 key fields per record, numbered
- If total > {len(display_rows)} → add: "Showing {len(display_rows)} of {total_rows}."

AVOID:
- Never say "Showing X of Y counts" for a single number
- Don't repeat identical values for every row — mention once
- Don't add unnecessary filler like "Need details? Just ask!"
  unless it genuinely helps
- If data has non-English words, translate them to English"""

    return await _call_openai_formatting(system_prompt, question, max_tokens=500)


def _validate_generated_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Generated SQL is empty.")

    lowered = cleaned.lower()
    if not lowered.startswith("select"):
        raise ValueError("Generated SQL is not a SELECT statement.")

    blocked_patterns = (
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\bdrop\b",
        r"\btruncate\b",
        r"\balter\b",
        r"\bcreate\b",
        r"\bgrant\b",
        r"\brevoke\b",
    )
    if any(re.search(pattern, lowered) for pattern in blocked_patterns):
        raise ValueError("Generated SQL includes disallowed operations.")

    if re.search(r"\bselect\s+\*", lowered):
        raise ValueError("Generated SQL uses SELECT * which is not allowed.")

    return cleaned


def _has_unsupported_distinct_window(sql: str) -> bool:
    return bool(
        re.search(
            r"\bcount\s*\(\s*distinct\b.*?\)\s*over\s*\(",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )


async def _fix_unsupported_postgres_constructs(sql: str, schema_blueprint: str) -> str:
    if not _has_unsupported_distinct_window(sql):
        return sql

    fixed_sql = await fix_sql(
        sql,
        "PostgreSQL does not support DISTINCT inside window functions. "
        "Rewrite using a SELECT DISTINCT subquery, then use COUNT(*) OVER () in the outer query.",
        schema_blueprint,
    )
    return _validate_generated_sql(fixed_sql)


async def _build_welcome_message(chat_id: str) -> str:
    """Build a contextual welcome message showing what data is available."""
    try:
        tenant = await get_tenant_by_chat_id(chat_id)
        if tenant is None:
            return (
                "Hi! I'm Botivate Bot.\n\n"
                "I couldn't find your account yet. "
                "Please use your onboarding link to get started."
            )

        credentials = await get_tenant_credentials(tenant.id)
        if not credentials or not credentials.schema_blueprint:
            return (
                f"Hi! Welcome to {tenant.company_name}'s assistant.\n\n"
                "Your database isn't configured yet. "
                "Please contact your admin to complete setup."
            )

        # Extract table names from blueprint
        table_names = _extract_table_names_from_blueprint(credentials.schema_blueprint)
        tables_display = ", ".join(table_names) if table_names else "your business data"

        return (
            f"Hi! I'm {tenant.company_name}'s data assistant.\n\n"
            f"I can query: {tables_display}\n\n"
            "Try asking me:\n"
            "\u2022 How many pending tasks?\n"
            "\u2022 Show tasks assigned to [name]\n"
            "\u2022 What is [name]'s email?\n"
            "\u2022 Count records by department\n\n"
            "Type /help anytime for more examples."
        )
    except Exception:
        return "Hi! I'm ready. Ask me a business question and I'll fetch it from your data."


async def handle_message(msg: BotMessage) -> None:
    # Show typing indicator immediately so the user knows we're working
    try:
        if msg.platform == Platform.TELEGRAM:
            from .platforms.telegram import send_typing
            await send_typing(msg.chat_id)
    except Exception:
        pass  # never block message processing for a typing indicator

    try:
        text_upper = msg.text.strip().upper()
        text_normalized = msg.text.strip().lower()

        token = None
        if msg.platform == Platform.TELEGRAM and text_upper.startswith("/START ") and len(msg.text.split()) > 1:
            token = msg.text.strip().split(" ", 1)[1]
        elif msg.platform == Platform.WHATSAPP and text_upper.startswith("START-"):
            token = msg.text.strip().split("-", 1)[1]

        if token:
            try:
                import jwt
                from .database import update_tenant_chat_id

                secret = os.getenv("ADMIN_SECRET_TOKEN", "")
                payload = jwt.decode(token, secret, algorithms=["HS256"])
                tenant_id = payload.get("tenant_id")
                if tenant_id:
                    await update_tenant_chat_id(tenant_id, msg.platform, msg.chat_id)
                    await send_reply(msg, "Welcome to Botivate! Your account is officially linked. How can I assist you today?")
                    return
            except Exception as e:
                logger.error("Failed to process magic link token: %s", e)
                await send_reply(msg, "Sorry, your onboarding link is invalid or expired. Please request a new link.")
                return

        if (msg.platform == Platform.TELEGRAM and text_normalized == "/start") or (
            msg.platform == Platform.WHATSAPP and text_normalized == "start"
        ):
            # Build a contextual welcome with available data
            welcome = await _build_welcome_message(msg.chat_id)
            await send_reply(msg, welcome)
            return

        if text_normalized in ("help", "/help"):
            await send_reply(
                msg,
                "Here are some things you can ask me:\n\n"
                "• How many pending tasks are there?\n"
                "• Show tasks assigned to [name]\n"
                "• What is the email of [name]?\n"
                "• Count of tasks by department\n"
                "• List all employees\n\n"
                "Just type your question naturally!",
            )
            return

        if await is_off_topic(msg.text):
            await send_reply(
                msg,
                "I can only help with your business data. Try questions like:\n\n"
                "• How many pending tasks?\n"
                "• Show tasks assigned to [name]\n"
                "• What is [person]'s email?\n"
                "• Count of records by department",
            )
            return

        tenant = await get_tenant_by_chat_id(msg.chat_id)
        if tenant is None:
            await send_reply(msg, ACCOUNT_NOT_FOUND_MESSAGE)
            return

        credentials = await get_tenant_credentials(tenant.id)
        if not credentials:
            await send_reply(msg, "Your database connection is not fully configured.")
            return

        if credentials.db_type.lower() == "postgresql":
            conversation_context_block = _build_conversation_context_block(msg)
            metadata_blueprint = credentials.schema_blueprint or "No semantic metadata available."
            try:
                runtime_schema, runtime_hints = await fetch_tenant_postgres_runtime_schema(tenant.id)
            except TenantDBConnectionError as schema_error:
                logger.error("[SCHEMA_ERR] tenant=%s error='%s'", tenant.id, schema_error)
                await send_reply(msg, DATABASE_CONNECTION_MESSAGE)
                return

            blueprint = (
                "SEMANTIC METADATA (metadata_analysis.json):\n"
                f"{metadata_blueprint}\n\n"
                "TECHNICAL POSTGRESQL SCHEMA FOR SQL GENERATION:\n"
                f"{runtime_schema}"
            )
            auto_schema_hints = "\n".join(
                part
                for part in (getattr(credentials, "auto_schema_hints", None), runtime_hints)
                if part and str(part).strip()
            )
            query_rows: list[dict[str, Any]] = []
            _generated_sql: str | None = None

            try:
                if detect_multi_table_query(msg.text):
                    table_names = _extract_table_names_from_blueprint(blueprint)
                    logger.info(f"[SQL_GEN] tenant={tenant.id} query='{msg.text}'")
                    if not table_names:
                        await send_reply(msg, "I couldn't find any tables in the schema blueprint for this tenant.")
                        return

                    combined_rows: list[dict[str, Any]] = []
                    for table_name in table_names:
                        table_sql = f"SELECT * FROM {table_name} LIMIT 2"
                        logger.info(f"[SQL_OUT] {table_sql}")
                        try:
                            rows = await execute_tenant_query(tenant.id, table_sql, allow_select_star=True)
                            for row in rows:
                                normalized = dict(row)
                                normalized["table_source"] = table_name
                                combined_rows.append(normalized)
                            logger.info(f"[SQL_OK] rows_returned={len(rows)}")
                        except TenantDBConnectionError as e:
                            logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
                            await send_reply(msg, DATABASE_CONNECTION_MESSAGE)
                            return
                        except (QueryExecutionError, SecurityError) as e:
                            logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
                    query_rows = combined_rows
                else:
                    # ── SQL GENERATION (Chain-of-Thought: Plan → SQL) ──
                    sql_query = await generate_sql_query(
                        tenant.company_name,
                        blueprint,
                        msg.text,
                        auto_schema_hints=auto_schema_hints,
                        tenant_id=tenant.id,
                        product_connection_id=None,
                        conversation_context_block=conversation_context_block,
                    )
                    sql_query = _maybe_expand_count_query_across_tables(sql_query, blueprint, msg.text)
                    sql_query = _validate_generated_sql(sql_query)
                    sql_query = await _fix_unsupported_postgres_constructs(sql_query, blueprint)
                    logger.info(f"[SQL_GEN] tenant={tenant.id} query='{msg.text}'")
                    logger.info(f"[SQL_OUT] {sql_query}")

                    # ── EXPLAIN VALIDATION (pre-flight check) ──
                    is_valid, explain_err = await explain_validate_sql(tenant.id, sql_query)
                    if not is_valid:
                        logger.warning(f"[EXPLAIN_FAIL] Pre-fixing SQL: {explain_err}")
                        sql_query = await fix_sql(sql_query, explain_err, blueprint)
                        sql_query = _maybe_expand_count_query_across_tables(sql_query, blueprint, msg.text)
                        sql_query = _validate_generated_sql(sql_query)
                        sql_query = await _fix_unsupported_postgres_constructs(sql_query, blueprint)
                        logger.info(f"[SQL_FIXED_BY_EXPLAIN] {sql_query}")

                    max_retries = 2
                    attempt = 0
                    _explain_passed = is_valid
                    while True:
                        try:
                            query_rows = await execute_tenant_query(tenant.id, sql_query)
                            logger.info(f"[SQL_OK] rows_returned={len(query_rows)}")
                            _generated_sql = sql_query
                            break
                        except TenantDBConnectionError as exec_error:
                            logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{exec_error}'")
                            await send_reply(msg, DATABASE_CONNECTION_MESSAGE)
                            return
                        except (QueryExecutionError, SecurityError) as exec_error:
                            final_error = str(exec_error)
                            logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{final_error}'")
                            if attempt >= max_retries:
                                logger.error(
                                    f"[SQL_FAILED] tenant={tenant.id} question='{msg.text}' "
                                    f"final_sql='{sql_query}' error='{final_error}'"
                                )
                                await send_reply(msg, RETRIEVAL_FAILURE_MESSAGE)
                                return
                            attempt += 1
                            sql_query = await fix_sql(sql_query, final_error, blueprint)
                            sql_query = _maybe_expand_count_query_across_tables(sql_query, blueprint, msg.text)
                            sql_query = _validate_generated_sql(sql_query)
                            sql_query = await _fix_unsupported_postgres_constructs(sql_query, blueprint)
                            logger.info(f"[SQL_OUT] {sql_query}")

                    # top_similarity value is in the preceding [FEW_SHOT] log line.
                    logger.info(
                        f"[QUERY_QUALITY] tenant={tenant.id} "
                        f"few_shot_used={_generated_sql is not None} "
                        f"top_similarity=see:[FEW_SHOT] "
                        f"plan_to_sql_match={_explain_passed} "
                        f"rows_returned={len(query_rows)} "
                        f"retries={attempt}"
                    )

            except Exception as sql_pipeline_error:
                logger.exception("[SQL_PIPELINE] Unhandled error in SQL pipeline for tenant %s", tenant.id)
                await send_reply(msg, RETRIEVAL_FAILURE_MESSAGE)
                return

            if not query_rows:
                await send_reply(msg, "I couldn't find any data matching your request.")
                return

            # ── REPLY FORMATTING (Mistral) ──
            try:
                reply = await format_sql_response(tenant.company_name, msg.text, query_rows)
                await send_reply(msg, reply or "I couldn't generate a response from the returned records.")
                _remember_conversation_context(msg, msg.text, reply or "", sql=_generated_sql)
            except Exception as fmt_error:
                logger.error(f"[FORMAT_ERR] {fmt_error}")
                await send_reply(msg, RETRIEVAL_FAILURE_MESSAGE)
                return

            # ── AUTO-FEEDBACK: store successful query for future few-shot retrieval ──
            if ENABLE_QUERY_LEARNING and _generated_sql is not None and query_rows:
                try:
                    await store_query_example(
                        tenant_id=tenant.id,
                        question=msg.text,
                        sql=_generated_sql,
                        product_connection_id=None,
                        verified_by="auto",
                    )
                except Exception as store_error:
                    logger.warning("[QUERY_LEARNING] Failed to store example: %s", store_error)
            return

        if credentials.db_type.lower() == "google_sheets":
            from cryptography.fernet import InvalidToken
            from .database import _decrypt_credential_value, fetch_google_sheet_runtime_context
            conversation_context_block = _build_conversation_context_block(msg)

            try:
                decrypted_url = _decrypt_credential_value(credentials.connection_url)
                sheet_id = decrypted_url.replace("google_sheets://", "")
                creds_json = _decrypt_credential_value(credentials.google_credentials) if credentials.google_credentials else None
            except (InvalidToken, Exception):
                await send_reply(msg, "Your Google Sheets credentials could not be decrypted. Please contact support.")
                return

            if not creds_json:
                await send_reply(msg, "Google Sheets credentials are not configured.")
                return

            try:
                live_context, gs_hints = fetch_google_sheet_runtime_context(sheet_id, creds_json)
            except Exception as e:
                logger.error("Google Sheets fetch failed: %s", e)
                await send_reply(msg, "I couldn't access your Google Sheet right now. Please try again.")
                return

            sheet_filters = _extract_sheet_value_filters(msg.text, gs_hints)
            metadata_blueprint = credentials.schema_blueprint or "No metadata analysis is stored for this Google Sheet yet."

            system_prompt = f"""You are {tenant.company_name}'s Google Sheets data analyst.
Answer using ONLY the GOOGLE SHEETS METADATA and LIVE DATA below.
Plain text only. No markdown, no bold, no tables. Keep it short: 3-8 lines.

GOOGLE SHEETS METADATA (metadata_analysis.json):
{metadata_blueprint}

LIVE GOOGLE SHEETS DATA:
{live_context}

{conversation_context_block}
RULES AND SEMANTIC HINTS:
{gs_hints}
{sheet_filters}
ANSWERING RULES:
- Treat each worksheet/tab as a table.
- Use the FULL DATA SNAPSHOT rows as the source of truth for answers.
- Sample rows are only examples of structure; do not answer from samples when full rows are available.
- If the question names a sheet/tab, use that sheet first. Otherwise choose the sheet whose description and headers best match the question.
- For lookup questions like "record/person/customer/order named X", filter by the primary name/title/ID column for the selected sheet. Do not apply unrelated columns such as Manager/Owner/Department unless the user explicitly asks for that relationship or filter.
- For counts, sums, averages, maximums, and minimums, calculate from the matching rows. Do not estimate.
- For lookup questions, return the exact value from the matching row and the most relevant fields around it.
- Format one matching record using the selected sheet's actual schema, not a hard-coded business template.
- Field order for a record:
  1. Primary identifier/name/title fields first, using primary_keys and column_descriptions from metadata when available.
  2. Fields directly requested by the user.
  3. important_columns from metadata, in the order they appear there.
  4. Remaining useful fields in the same left-to-right order as the sheet headers.
- Group related fields only when the schema clearly supports it; otherwise use compact "Column: value" lines.
- For multiple records, number each record and keep the same schema-derived field order.
- For pending/incomplete/not done, apply the Status/Pending hints. Use blank completion/submission dates only when the hint says blank means pending.
- If a sheet says its data snapshot is truncated and the question needs all rows, say the exact answer needs a full snapshot instead of inventing a number.
- If the answer is not present in the context, say you could not find it in the sheet.

AVOID:
- Never force employee/HR-specific labels onto other schemas.
- Never mention filters that produced no match if another matching row exists through a better primary name/title/ID field.
- Never say "Based on the data provided".
- Never repeat every column name as a label on every row.
- Never add filler like "Let me know if you need more!"

LANGUAGE RULE:
Look at this exact user question: "{msg.text}"
Reply in the exact same language as that question. Database values in other languages must NOT influence your reply language.

USER QUESTION: {msg.text}""".strip()
            reply = await _call_openai_formatting(system_prompt, msg.text, max_tokens=600)
            await send_reply(msg, reply or "I couldn't generate a response.")
            _remember_conversation_context(msg, msg.text, reply or "")
            return

        await send_reply(msg, "Unsupported tenant data source configuration.")
    except Exception:
        logger.exception("Failed to process customer message for chat_id %s", msg.chat_id)


__all__ = ["handle_message", "fix_sql", "generate_sql_query"]
