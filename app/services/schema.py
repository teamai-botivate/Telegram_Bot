from __future__ import annotations

import re

from .core import logger


# ── Sheet value extraction ───────────────────────────────────────────────────

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


# ── Schema parsing ───────────────────────────────────────────────────────────

def _extract_table_names_from_blueprint(schema_blueprint: str) -> list[str]:
    return re.findall(r"^Table `([^`]+)`", schema_blueprint, flags=re.MULTILINE)


def _extract_columns_for_table(schema_blueprint: str, table_name: str) -> list[str]:
    """Extract column names for a specific table from the schema blueprint string."""
    pattern = rf"Table `{re.escape(table_name)}`.*?Columns:\s*([^\n]+)"
    match = re.search(pattern, schema_blueprint, re.DOTALL)
    if not match:
        return []
    columns_str = match.group(1)
    # Parse "col_name (type), col_name2 (type)" format.
    return re.findall(r"(\w+)\s*\(", columns_str)


def _extract_tables_with_column(schema_blueprint: str, column_name: str) -> list[str]:
    target = column_name.lower()
    matches: list[str] = []
    for table_name in _extract_table_names_from_blueprint(schema_blueprint):
        columns = [column.lower() for column in _extract_columns_for_table(schema_blueprint, table_name)]
        if target in columns:
            matches.append(table_name)
    return matches


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


# ── Count query expansion ────────────────────────────────────────────────────

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


# ── SQL validation ───────────────────────────────────────────────────────────

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
    from .sqlgen import fix_sql

    if not _has_unsupported_distinct_window(sql):
        return sql

    fixed_sql = await fix_sql(
        sql,
        "PostgreSQL does not support DISTINCT inside window functions. "
        "Rewrite using a SELECT DISTINCT subquery, then use COUNT(*) OVER () in the outer query.",
        schema_blueprint,
    )
    return _validate_generated_sql(fixed_sql)