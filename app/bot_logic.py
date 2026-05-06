from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

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
    execute_credential_query,
    execute_tenant_query,
    fetch_credential_postgres_runtime_schema,
    fetch_tenant_postgres_runtime_schema,
    find_registered_client_by_chat,
    get_tenant_by_chat_id,
    get_tenant_credentials,
    get_tenant_credentials_all,
    retrieve_similar_examples,
    store_query_example,
)
from .platforms.base import BotMessage, Platform, send_reply

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SQL_GENERATION_MODEL = os.getenv("SQL_GENERATION_MODEL", "gpt-5.2")
RESPONSE_FORMAT_MODEL = os.getenv("RESPONSE_FORMAT_MODEL", "gpt-5.2")
DB_ROUTER_MODEL = os.getenv("DB_ROUTER_MODEL", "gpt-5.2")
OFF_TOPIC_CLASSIFIER_MODEL = os.getenv("OFF_TOPIC_CLASSIFIER_MODEL", "gpt-5.2")
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


async def _call_openai_classifier(system_prompt: str, user_prompt: str) -> str:
    client = _get_openai_client()
    completion = await client.chat.completions.create(
        model=OFF_TOPIC_CLASSIFIER_MODEL,
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
            
    # Remove any hallucinated prefix text (e.g., "[RAW SQL ONLY...]")
    # by finding the first SELECT or WITH
    match = re.search(r"\b(SELECT|WITH)\b", cleaned, re.IGNORECASE)
    if match:
        cleaned = cleaned[match.start():]
        
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
    """Fast local heuristic to reject obvious small talk/junk. 
    Eliminates a 2-3s LLM round trip. If a junk message slips through, 
    the SQL pipeline will safely fail to find data anyway.
    """
    text_lower = text.strip().lower()
    
    if len(text_lower) < 2:
        return True
        
    # Common small talk (exact or near-exact match)
    small_talk = {
        "hi", "hello", "hey", "good morning", "good evening", "good afternoon",
        "how are you", "how are you?", "who are you", "who are you?", "what are you",
        "thanks", "thank you", "bye", "goodbye", "ok", "okay", "test", "testing", "ping"
    }
    if text_lower in small_talk:
        return True
        
    # Common LLM jailbreak / out-of-bounds prefixes
    junk_patterns = [
        r"^tell me a joke",
        r"^what is the weather",
        r"^write a poem",
        r"^write code",
        r"^who is the president",
        r"^how to make",
        r"^recipe for",
        r"^sing a song",
        r"^ignore all previous"
    ]
    if any(re.search(p, text_lower) for p in junk_patterns):
        return True
        
    return False


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


async def generate_sql_query(
    company_name: str,
    schema_blueprint: str,
    question: str,
    auto_schema_hints: str | None = None,
    tenant_id: Any = None,
    product_connection_id: Any = None,
    conversation_context_block: str = "",
    precomputed_embedding: list[float] | None = None,
) -> str:
    """Single-step Chain-of-Thought SQL generation.
    Plans the query and outputs the final SQL in one LLM call.
    Fully multi-tenant — uses the tenant's own schema_blueprint and hints.
    """
    # ── Few-shot retrieval (best-effort; never raises) ──
    examples: list[dict[str, Any]] = []
    if tenant_id is not None:
        try:
            examples = await retrieve_similar_examples(
                tenant_id, question, product_connection_id=product_connection_id, limit=5,
                precomputed_embedding=precomputed_embedding,
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

    entities = _extract_entities(question)
    entities_json = json.dumps(entities, default=str)
    dynamic_aliases = build_table_aliases(schema_blueprint)
    person_filter = _extract_name_filters(question, auto_schema_hints)

    if auto_schema_hints and auto_schema_hints.strip():
        hints_section = auto_schema_hints.strip()
    else:
        hints_section = "No auto-inferred schema rules available."

    system_prompt = f"""You are an expert PostgreSQL database analyst for {company_name}.
Analyze the question and schema, then think step-by-step to build a correct SELECT query.

SCHEMA:
{schema_blueprint}
{similar_questions_block}
{few_shot_block}
{conversation_context_block}
TABLE ALIASES: {dynamic_aliases}

RULES:
{hints_section}
{person_filter}

PLANNING INSTRUCTIONS:
1. DISTINCT: If the question asks "how many people/employees/workers" or "list/name the people" — use COUNT(DISTINCT column) or SELECT DISTINCT.
2. Only use tables and columns that EXIST in the schema above.
3. PENDING/STATUS: Look at the schema sample values. Use IS NULL if hinted.
4. If a PERSON FILTER section appears above, you MUST include those exact WHERE conditions.
5. For boolean columns → TRUE/FALSE, never text.
6. Ignore tables starting with "extensions." or "pg_".
7. MULTI-TABLE: If the question asks about multiple tables, query each table separately with UNION ALL.
8. USER TEXT VALUES: Use LOWER(TRIM(text_column)) = LOWER(TRIM('value')).
9. FOLLOW-UP QUESTIONS: Preserve relevant context.
10. COUNT + WHO/BY WHOM: GROUP BY appropriately.

SQL REQUIREMENTS:
- Declare aliases: FROM table AS alias
- Never use SELECT * — list columns explicitly
- ILIKE for text searches
- IS NULL / IS NOT NULL for nullable dates
- LEFT JOIN preferred over INNER JOIN
- LIMIT must be included
- For UNION ALL, cast columns to ::text
- PostgreSQL does not support COUNT(DISTINCT ...) OVER ().
- If the question asks for separate counts, return as separate named columns or rows.

OUTPUT FORMAT:
You MUST format your output exactly like this:
<thought_process>
1. Table(s): ...
2. Filter(s): ...
3. Select/Agg: ...
</thought_process>
<sql>
YOUR_RAW_SQL_QUERY_HERE
</sql>

QUESTION: {question}
ENTITIES: {entities_json}""".strip()

    user_prompt = f"Generate the query plan and SQL query for: {question}"
    response_text = await _call_openai_sql(system_prompt, user_prompt)
    
    # Extract just the SQL portion
    sql_match = re.search(r"<sql>\s*(.*?)\s*</sql>", response_text, re.DOTALL | re.IGNORECASE)
    if sql_match:
        raw_sql = sql_match.group(1)
        # Log the thought process for debugging
        thought_match = re.search(r"<thought_process>\s*(.*?)\s*</thought_process>", response_text, re.DOTALL | re.IGNORECASE)
        if thought_match:
            logger.info("[SQL_PLAN] %s", thought_match.group(1).replace("\n", " | "))
    else:
        # Fallback if the model didn't use the tags
        raw_sql = response_text
        if "<thought_process>" in raw_sql:
            raw_sql = re.sub(r"<thought_process>.*?</thought_process>", "", raw_sql, flags=re.DOTALL | re.IGNORECASE).strip()
            
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
- Text/name equality: use LOWER(TRIM(column)) = LOWER(TRIM('value'))
  instead of raw column = 'value' when matching user-provided names or labels
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
    if not (lowered.startswith("select") or lowered.startswith("with")):
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


def _summarize_credential_for_router(credential: Any) -> dict[str, Any]:
    """Build the compact per-DB summary the router LLM sees."""
    table_names: list[str] = []
    blueprint = getattr(credential, "schema_blueprint", None)
    if blueprint:
        table_names = _extract_table_names_from_blueprint(blueprint)
    cred_id = str(getattr(credential, "id", "credential"))
    slug = getattr(credential, "product_slug", None) or cred_id
    display = getattr(credential, "display_name", None) or slug or "Database"
    return {
        "product_slug": slug,
        "display_name": display,
        "db_type": (getattr(credential, "db_type", "") or "").lower(),
        "table_names": table_names[:25],  # cap to keep prompt small
    }


async def route_question_to_database(
    tenant_id: Any, question: str
) -> list[Any]:
    """Pick which of the tenant's databases should answer the question.

    - 0 connections → returns [].
    - 1 connection → returns it. No LLM call.
    - 2+ connections → asks gpt-4o-mini for the slug(s); falls back to all DBs on failure.
    """
    credentials = await get_tenant_credentials_all(tenant_id)

    if not credentials:
        return []

    if len(credentials) == 1:
        # Fast path: zero added latency, zero LLM tokens, no behavior change for
        # single-DB tenants.
        return credentials

    summaries = [_summarize_credential_for_router(c) for c in credentials]
    slug_to_credential: dict[str, Any] = {}
    for cred, summary in zip(credentials, summaries):
        slug_to_credential[summary["product_slug"]] = cred

    user_prompt = (
        f"User question: {question}\n\n"
        f"Available databases for this tenant:\n"
        f"{json.dumps(summaries, indent=2)}\n\n"
        "Which database(s) are needed to answer this question? Return JSON only:\n"
        '{"databases": ["product_slug1", "product_slug2"], "reason": "..."}\n'
        "- Return 1 slug if the question clearly fits one database.\n"
        "- Return 2+ slugs ONLY if the question explicitly spans multiple databases.\n"
        "- If unsure, return the single best match."
    )

    chosen_slugs: list[str] = []
    reason = ""
    try:
        client = _get_openai_client()
        completion = await client.chat.completions.create(
            model=DB_ROUTER_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a database router. Given a user question and a list of "
                        "available databases, pick which database(s) should be queried. "
                        "Output JSON only."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        chosen_slugs = [str(s) for s in parsed.get("databases", []) if isinstance(s, str)]
        reason = str(parsed.get("reason", ""))[:300]
    except Exception as exc:
        logger.warning("[ROUTE] LLM router failed; falling back to all DBs: %s", exc)
        logger.info(
            "[ROUTE] tenant=%s dbs_available=%d dbs_chosen=%s reason='router_error'",
            tenant_id, len(credentials), [s["product_slug"] for s in summaries],
        )
        return credentials

    matched: list[Any] = []
    seen: set[str] = set()
    for slug in chosen_slugs:
        if slug in slug_to_credential and slug not in seen:
            matched.append(slug_to_credential[slug])
            seen.add(slug)

    if not matched:
        # LLM returned slugs that don't match any DB — treat as router failure.
        logger.warning(
            "[ROUTE] LLM returned no valid slugs (got %s); falling back to all DBs.",
            chosen_slugs,
        )
        logger.info(
            "[ROUTE] tenant=%s dbs_available=%d dbs_chosen=%s reason='no_valid_slugs'",
            tenant_id, len(credentials), [s["product_slug"] for s in summaries],
        )
        return credentials

    logger.info(
        "[ROUTE] tenant=%s dbs_available=%d dbs_chosen=%s reason=%r",
        tenant_id, len(credentials),
        [getattr(c, "product_slug", None) or str(getattr(c, "id", "?")) for c in matched],
        reason,
    )
    return matched


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


async def _run_postgres_pipeline_for_credential(
    msg: BotMessage, tenant: Any, credential: Any
) -> dict[str, Any]:
    """Run the SQL gen → execute → format pipeline against ONE Postgres credential.

    Returns a dict with keys:
      status: 'ok' | 'empty' | 'error' | 'connection_error'
      reply:  formatted answer string when status == 'ok'
      sql:    generated SQL string when status == 'ok'
    """
    conversation_context_block = _build_conversation_context_block(msg)
    metadata_blueprint = credential.schema_blueprint or "No semantic metadata available."
    cred_label = (
        getattr(credential, "display_name", None)
        or getattr(credential, "product_slug", None)
        or str(getattr(credential, "id", "credential"))
    )

    # ── Run schema fetch and question embedding concurrently ──
    from .embeddings import embed_text

    async def _safe_embed() -> list[float] | None:
        try:
            return await embed_text(msg.text)
        except Exception as exc:
            logger.warning("[EMBED_PRE] failed: %s", exc)
            return None

    try:
        (runtime_schema, runtime_hints), question_embedding = await asyncio.gather(
            fetch_credential_postgres_runtime_schema(credential),
            _safe_embed(),
        )
    except TenantDBConnectionError as schema_error:
        logger.error("[SCHEMA_ERR] credential=%s error='%s'", credential.id, schema_error)
        return {"status": "connection_error"}

    blueprint = (
        "SEMANTIC METADATA (metadata_analysis.json):\n"
        f"{metadata_blueprint}\n\n"
        "TECHNICAL POSTGRESQL SCHEMA FOR SQL GENERATION:\n"
        f"{runtime_schema}"
    )
    auto_schema_hints = "\n".join(
        part
        for part in (getattr(credential, "auto_schema_hints", None), runtime_hints)
        if part and str(part).strip()
    )
    query_rows: list[dict[str, Any]] = []
    _generated_sql: str | None = None

    try:
        if detect_multi_table_query(msg.text):
            table_names = _extract_table_names_from_blueprint(blueprint)
            logger.info(f"[SQL_GEN] credential={credential.id} query='{msg.text}'")
            if not table_names:
                logger.info("[SQL_PIPELINE] No tables in blueprint for credential=%s", credential.id)
                return {"status": "empty"}

            combined_rows: list[dict[str, Any]] = []
            for table_name in table_names:
                table_sql = f"SELECT * FROM {table_name} LIMIT 2"
                logger.info(f"[SQL_OUT] {table_sql}")
                try:
                    rows = await execute_credential_query(
                        credential, table_sql, allow_select_star=True
                    )
                    for row in rows:
                        normalized = dict(row)
                        normalized["table_source"] = table_name
                        combined_rows.append(normalized)
                    logger.info(f"[SQL_OK] rows_returned={len(rows)}")
                except TenantDBConnectionError as e:
                    logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
                    return {"status": "connection_error"}
                except (QueryExecutionError, SecurityError) as e:
                    logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
            query_rows = combined_rows
        else:
            sql_query = await generate_sql_query(
                tenant.company_name,
                blueprint,
                msg.text,
                auto_schema_hints=auto_schema_hints,
                tenant_id=tenant.id,
                product_connection_id=None,
                conversation_context_block=conversation_context_block,
                precomputed_embedding=question_embedding,
            )
            sql_query = _maybe_expand_count_query_across_tables(sql_query, blueprint, msg.text)
            sql_query = _validate_generated_sql(sql_query)
            sql_query = await _fix_unsupported_postgres_constructs(sql_query, blueprint)
            logger.info(f"[SQL_GEN] credential={credential.id} query='{msg.text}'")
            logger.info(f"[SQL_OUT] {sql_query}")

            max_retries = 2
            attempt = 0
            while True:
                try:
                    query_rows = await execute_credential_query(credential, sql_query)
                    logger.info(f"[SQL_OK] rows_returned={len(query_rows)}")
                    _generated_sql = sql_query
                    break
                except TenantDBConnectionError as exec_error:
                    logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{exec_error}'")
                    return {"status": "connection_error"}
                except (QueryExecutionError, SecurityError) as exec_error:
                    final_error = str(exec_error)
                    logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{final_error}'")
                    if attempt >= max_retries:
                        logger.error(
                            f"[SQL_FAILED] credential={credential.id} question='{msg.text}' "
                            f"final_sql='{sql_query}' error='{final_error}'"
                        )
                        return {"status": "error"}
                    attempt += 1
                    sql_query = await fix_sql(sql_query, final_error, blueprint)
                    sql_query = _maybe_expand_count_query_across_tables(sql_query, blueprint, msg.text)
                    sql_query = _validate_generated_sql(sql_query)
                    sql_query = await _fix_unsupported_postgres_constructs(sql_query, blueprint)
                    logger.info(f"[SQL_OUT] {sql_query}")

            logger.info(
                f"[QUERY_QUALITY] tenant={tenant.id} credential={credential.id} "
                f"few_shot_used={_generated_sql is not None} "
                f"top_similarity=see:[FEW_SHOT] "
                f"rows_returned={len(query_rows)} "
                f"retries={attempt}"
            )
    except Exception:
        logger.exception("[SQL_PIPELINE] Unhandled error for credential %s", credential.id)
        return {"status": "error"}

    if not query_rows:
        return {"status": "empty"}

    try:
        reply = await format_sql_response(tenant.company_name, msg.text, query_rows)
    except Exception as fmt_error:
        logger.error(f"[FORMAT_ERR] credential={credential.id} {fmt_error}")
        return {"status": "error"}

    if not reply:
        return {"status": "error"}

    _remember_conversation_context(msg, msg.text, reply, sql=_generated_sql)

    if ENABLE_QUERY_LEARNING and _generated_sql is not None and query_rows:
        async def _store_bg() -> None:
            try:
                await store_query_example(
                    tenant_id=tenant.id,
                    question=msg.text,
                    sql=_generated_sql,
                    product_connection_id=None,
                    verified_by="auto",
                )
            except Exception as store_error:
                logger.warning("[QUERY_LEARNING] Failed to store example for %s: %s", cred_label, store_error)
        asyncio.create_task(_store_bg())

    return {"status": "ok", "reply": reply, "sql": _generated_sql}


async def _run_sheets_pipeline_for_credential(
    msg: BotMessage, tenant: Any, credential: Any
) -> dict[str, Any]:
    """Run the Google Sheets answer pipeline against ONE Sheets credential."""
    from cryptography.fernet import InvalidToken
    from .database import _decrypt_credential_value, fetch_google_sheet_runtime_context

    conversation_context_block = _build_conversation_context_block(msg)
    cred_label = (
        getattr(credential, "display_name", None)
        or getattr(credential, "product_slug", None)
        or str(getattr(credential, "id", "credential"))
    )

    try:
        decrypted_url = _decrypt_credential_value(credential.connection_url)
        sheet_id = decrypted_url.replace("google_sheets://", "")
        creds_json = (
            _decrypt_credential_value(credential.google_credentials)
            if credential.google_credentials
            else None
        )
    except (InvalidToken, Exception):
        logger.error("[SHEETS_DECRYPT_ERR] credential=%s", credential.id)
        return {"status": "connection_error"}

    if not creds_json:
        logger.warning("[SHEETS] credentials missing for credential=%s", credential.id)
        return {"status": "error"}

    try:
        live_context, gs_hints = await fetch_google_sheet_runtime_context(sheet_id, creds_json, question=msg.text)
    except Exception as e:
        logger.error("Google Sheets fetch failed for credential=%s: %s", credential.id, e)
        return {"status": "connection_error"}

    sheet_filters = _extract_sheet_value_filters(msg.text, gs_hints)
    metadata_blueprint = credential.schema_blueprint or "No metadata analysis is stored for this Google Sheet yet."

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
- If TARGETED ROW MATCHES are present, use those per-sheet counts/rows first.
  They are computed from all worksheet rows before the displayed snapshot is truncated.
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

    try:
        reply = await _call_openai_formatting(system_prompt, msg.text, max_tokens=600)
    except Exception as fmt_error:
        logger.error(f"[FORMAT_ERR] sheets credential={credential.id} {fmt_error}")
        return {"status": "error"}

    if not reply:
        return {"status": "error"}

    _remember_conversation_context(msg, msg.text, reply)
    return {"status": "ok", "reply": reply, "sql": None}


async def _handle_unboarded_client(msg: BotMessage, registered_client: Any) -> None:
    """Send a personalised onboarding link to a registered-but-not-yet-set-up client.

    If some products are already connected, sends an "add remaining DB" link instead.
    All failures are caught so they never surface as an unhandled exception to the caller.
    """
    from .auth.onboarding_jwt import InvalidOnboardingTokenError, build_form_url, issue_token

    try:
        purchased: list[dict[str, Any]] = list(registered_client.purchased_products or [])

        # Determine which products already have a credential row in NeonDB.
        connected_slugs: set[str] = set()
        if registered_client.tenant_id is not None:
            all_creds = await get_tenant_credentials(registered_client.tenant_id)
            if all_creds is not None:
                # get_tenant_credentials returns a single row (legacy); fetch all rows instead.
                from .database import session_factory
                from .models import TenantDBCredential as _Cred
                from sqlalchemy import select as _select
                async with session_factory() as _s:
                    _res = await _s.execute(
                        _select(_Cred).where(_Cred.tenant_id == registered_client.tenant_id)
                    )
                    for cred_row in _res.scalars().all():
                        if cred_row.product_slug:
                            connected_slugs.add(cred_row.product_slug)

        unconnected = [p for p in purchased if p.get("slug") not in connected_slugs]

        if not unconnected:
            # All purchased products already have a credential — shouldn't normally reach
            # here, but handle gracefully.
            await send_reply(
                msg,
                f"Hi {registered_client.contact_name}! Your account is already set up. "
                "Try asking me a business question.",
            )
            return

        if not connected_slugs:
            # Nothing connected yet — initial setup link.
            purpose = "initial_setup"
            product_slug_for_token = None
            first_product = unconnected[0].get("display_name") or unconnected[0].get("slug", "")
        else:
            # Partially onboarded — link for the first remaining product.
            purpose = "add_database"
            product_slug_for_token = unconnected[0].get("slug")
            first_product = unconnected[0].get("display_name") or product_slug_for_token or ""

        try:
            token, _ = await issue_token(
                registered_client_id=registered_client.id,
                purpose=purpose,
                product_slug=product_slug_for_token,
            )
            form_url = build_form_url(token)
        except RuntimeError as cfg_err:
            logger.error("[ONBOARDING] Configuration error for client %s: %s", registered_client.id, cfg_err)
            await send_reply(
                msg,
                "Your account is registered but onboarding isn't configured yet. "
                "Please contact the Botivate team.",
            )
            return

        if not connected_slugs:
            reply = (
                f"Welcome to Botivate, {registered_client.contact_name} "
                f"from {registered_client.company_name}! "
                f"To connect your database, please use this link "
                f"(expires in 30 minutes):\n{form_url}"
            )
        else:
            reply = (
                f"You still need to connect your {first_product} database. "
                f"Use this link:\n{form_url}"
            )

        logger.info(
            "[ONBOARDING] Issued %s token for registered_client=%s purpose=%s product=%s",
            purpose,
            registered_client.id,
            purpose,
            product_slug_for_token,
        )
        await send_reply(msg, reply)

    except Exception:
        logger.exception(
            "[ONBOARDING] Unexpected error handling unboarded client chat_id=%s", msg.chat_id
        )
        await send_reply(
            msg,
            "Your account is registered but setup isn't complete yet. "
            "Please contact the Botivate team.",
        )


async def _handle_adddb_command(msg: BotMessage) -> None:
    """Handle the /adddb command — let an existing or not-yet-onboarded client connect a database."""
    from .auth.onboarding_jwt import build_form_url, issue_token

    # registered_client is the source of truth for purchased_products.
    # A tenant row is NOT required — unboarded clients (tenant_id=None) can also use /adddb.
    registered_client = await find_registered_client_by_chat(
        msg.platform.value, msg.chat_id
    )
    if registered_client is None:
        await send_reply(
            msg,
            "I couldn't find your account. Please contact the Botivate team to get registered.",
        )
        return

    purchased: list[dict[str, Any]] = list(registered_client.purchased_products or [])
    if not purchased:
        await send_reply(
            msg,
            "No purchased products are associated with your account. "
            "Please contact the Botivate team.",
        )
        return

    # Determine which slugs already have a credential row (only possible if tenant exists)
    connected_slugs: set[str] = set()
    tenant = await get_tenant_by_chat_id(msg.chat_id)
    if tenant is not None:
        all_creds = await get_tenant_credentials_all(tenant.id)
        connected_slugs = {
            getattr(c, "product_slug", None)
            for c in all_creds
            if getattr(c, "product_slug", None)
        }

    unconnected = [p for p in purchased if p.get("slug") not in connected_slugs]

    if not unconnected:
        await send_reply(
            msg,
            "All your purchased products already have databases connected. "
            "Contact the Botivate team to add more products.",
        )
        return

    if len(unconnected) == 1:
        slug = unconnected[0].get("slug")
        display = unconnected[0].get("display_name") or slug or "database"
        purpose = "initial_setup" if tenant is None else "add_database"
        try:
            token, _ = await issue_token(
                registered_client_id=registered_client.id,
                purpose=purpose,
                product_slug=slug,
            )
            form_url = build_form_url(token)
        except RuntimeError as cfg_err:
            logger.error("[ADDDB] Configuration error for client %s: %s", registered_client.id, cfg_err)
            await send_reply(msg, "Onboarding is not configured yet. Please contact the Botivate team.")
            return
        await send_reply(
            msg,
            f"To connect your {display} database, use this link (expires in 30 minutes):\n{form_url}",
        )
        return

    # 2+ unconnected products — let the user pick
    if msg.platform == Platform.TELEGRAM:
        from .platforms.telegram import send_message_with_keyboard

        buttons = [
            [{"text": p.get("display_name") or p.get("slug", ""), "callback_data": f"adddb_product:{p.get('slug', '')}"}]
            for p in unconnected
        ]
        try:
            await send_message_with_keyboard(
                msg.chat_id,
                "Which database would you like to connect?",
                inline_keyboard=buttons,
            )
        except Exception as exc:
            logger.error("[ADDDB] Failed to send keyboard: %s", exc)
            await send_reply(msg, "Something went wrong. Please try again.")
    else:
        # WhatsApp: numbered text list
        lines = ["Which database would you like to connect? Reply with a number:"]
        for i, p in enumerate(unconnected, start=1):
            lines.append(f"{i}. {p.get('display_name') or p.get('slug', '')}")
        await send_reply(msg, "\n".join(lines))


async def handle_adddb_callback(
    chat_id: str, callback_query_id: str, callback_data: str
) -> None:
    """Handle Telegram inline keyboard callback for /adddb product selection."""
    from .auth.onboarding_jwt import build_form_url, issue_token
    from .platforms.telegram import answer_callback_query

    await answer_callback_query(callback_query_id)

    slug = callback_data.removeprefix("adddb_product:").strip()
    if not slug:
        return

    registered_client = await find_registered_client_by_chat(Platform.TELEGRAM.value, chat_id)
    if registered_client is None:
        return

    purchased: list[dict[str, Any]] = list(registered_client.purchased_products or [])
    product = next((p for p in purchased if p.get("slug") == slug), None)
    display = (product.get("display_name") if product else None) or slug

    try:
        token, _ = await issue_token(
            registered_client_id=registered_client.id,
            purpose="add_database",
            product_slug=slug,
        )
        form_url = build_form_url(token)
    except Exception as exc:
        logger.error("[ADDDB_CB] Token issuance failed for chat_id=%s slug=%s: %s", chat_id, slug, exc)
        from .platforms.telegram import send_message
        await send_message(chat_id, "Something went wrong. Please try again.")
        return

    from .platforms.telegram import send_message
    await send_message(
        chat_id,
        f"To connect your {display} database, use this link (expires in 30 minutes):\n{form_url}",
    )


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

        if (msg.platform == Platform.TELEGRAM and text_normalized == "/adddb") or (
            msg.platform == Platform.WHATSAPP and text_normalized == "adddb"
        ):
            await _handle_adddb_command(msg)
            return

        # ── Run off-topic check and tenant lookup concurrently ────────────────
        off_topic_result, tenant = await asyncio.gather(
            is_off_topic(msg.text),
            get_tenant_by_chat_id(msg.chat_id),
        )

        if off_topic_result:
            await send_reply(
                msg,
                "I can only help with your business data. Try questions like:\n\n"
                "• How many pending tasks?\n"
                "• Show tasks assigned to [name]\n"
                "• What is [person]'s email?\n"
                "• Count of records by department",
            )
            return

        # ── Tier 1: fully onboarded tenant ───────────────────────────────────
        if tenant is None:
            # ── Tier 2: registered but not yet onboarded ──────────────────
            registered_client = await find_registered_client_by_chat(
                msg.platform.value, msg.chat_id
            )

            if registered_client is not None:
                await _handle_unboarded_client(msg, registered_client)
                return

            # ── Tier 3: not registered at all ─────────────────────────────
            await send_reply(
                msg,
                "Hi! I couldn't find your account. "
                "Please contact the Botivate team to get registered.",
            )
            return

        # ── Pick which DB(s) to query ───────────────────────────────────────
        connections = await route_question_to_database(tenant.id, msg.text)
        if not connections:
            await send_reply(msg, "I couldn't determine which database to query. Please rephrase.")
            return

        single_db = len(connections) == 1
        sections: list[tuple[str, str]] = []  # (display_name, reply_text)
        outcomes: list[str] = []

        for credential in connections:
            db_type = (credential.db_type or "").lower()
            if db_type == "postgresql":
                outcome = await _run_postgres_pipeline_for_credential(msg, tenant, credential)
            elif db_type == "google_sheets":
                outcome = await _run_sheets_pipeline_for_credential(msg, tenant, credential)
            else:
                logger.warning(
                    "[SQL_PIPELINE] Unsupported db_type=%r for credential %s; skipping.",
                    db_type, credential.id,
                )
                continue

            status = outcome.get("status", "error")
            outcomes.append(status)
            if status == "ok" and outcome.get("reply"):
                sections.append(
                    (
                        getattr(credential, "display_name", None)
                        or getattr(credential, "product_slug", None)
                        or "Database",
                        outcome["reply"],
                    )
                )

        if not sections:
            # Pick the most-informative reply for the user. Connection errors win over
            # soft errors win over empty results.
            if "connection_error" in outcomes:
                await send_reply(msg, DATABASE_CONNECTION_MESSAGE)
            elif "error" in outcomes:
                await send_reply(msg, RETRIEVAL_FAILURE_MESSAGE)
            else:
                await send_reply(msg, "I couldn't find any data matching your request.")
            return

        if single_db:
            # Preserve exact pre-routing UX: no attribution prefix.
            await send_reply(msg, sections[0][1])
        else:
            combined = "\n\n".join(f"From {name}:\n{body}" for name, body in sections)
            await send_reply(msg, combined)
    except Exception:
        logger.exception("Failed to process customer message for chat_id %s", msg.chat_id)


__all__ = ["handle_message", "handle_adddb_callback", "fix_sql", "generate_sql_query"]
