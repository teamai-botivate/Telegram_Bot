from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .core import logger
from .schema import _extract_table_names_from_blueprint
from .llm import _call_openai_sql
from app.database import retrieve_similar_examples


# ── Multi-table detection ────────────────────────────────────────────────────

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


# ── SQL generation ───────────────────────────────────────────────────────────

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
- Do NOT add a LIMIT clause unless the user explicitly asks for a top-N limit.
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


# ── SQL repair ───────────────────────────────────────────────────────────────

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


# ── Entity extraction ────────────────────────────────────────────────────────

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


# ── Name filter extraction ──────────────────────────────────────────────────

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


# ── Table alias generation ───────────────────────────────────────────────────

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


# ── Code fence stripping ────────────────────────────────────────────────────

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