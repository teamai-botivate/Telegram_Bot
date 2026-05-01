"""
Seed few-shot examples for the "Minutes of Meeting" Botivate product.

Usage:
    # Dry run (default) — validates examples against the tenant's live schema:
    python scripts/seed_examples_minutes_of_meeting.py --tenant-id <uuid>

    # Actually seed after reviewing the dry-run output:
    python scripts/seed_examples_minutes_of_meeting.py --tenant-id <uuid> --apply

Requires ADMIN_SECRET_TOKEN and DATABASE_URL in .env (or environment).
The script reads the tenant's schema_blueprint from the meta DB to validate
that every table and column referenced in each example actually exists.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

ADMIN_SECRET_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# Canonical seed examples for Minutes of Meeting
# ---------------------------------------------------------------------------
# Column names must match whatever the tenant's schema_blueprint reports.
# The script validates each example's referenced tables/columns at runtime
# and prints a warning (dry-run) or skips (apply) any that don't pass.
#
# Pattern legend used in SQL below:
#   completed_at IS NULL     → task/action item is still pending
#   completed_at IS NOT NULL → task/action item is done
#   due_date <= CURRENT_DATE → overdue or due today
# ---------------------------------------------------------------------------

SEED_EXAMPLES: list[dict] = [
    # ── Pending / completion counts ─────────────────────────────────────────
    {
        "question": "How many pending tasks are there?",
        "sql": (
            "SELECT COUNT(*) AS pending_count"
            " FROM tbl_tasks AS t"
            " WHERE t.completed_at IS NULL"
        ),
    },
    {
        "question": "How many tasks have been completed?",
        "sql": (
            "SELECT COUNT(*) AS completed_count"
            " FROM tbl_tasks AS t"
            " WHERE t.completed_at IS NOT NULL"
        ),
    },
    {
        "question": "What is the count of pending vs completed tasks?",
        "sql": (
            "SELECT"
            "  CASE WHEN completed_at IS NULL THEN 'Pending' ELSE 'Completed' END AS status,"
            "  COUNT(*) AS count"
            " FROM tbl_tasks"
            " GROUP BY 1"
            " ORDER BY 1"
        ),
    },
    # ── Tasks assigned to a person ──────────────────────────────────────────
    {
        "question": "Show all tasks assigned to Rahul",
        "sql": (
            "SELECT t.task_name, t.due_date, t.completed_at"
            " FROM tbl_tasks AS t"
            " WHERE t.assigned_to ILIKE '%Rahul%'"
            " ORDER BY t.due_date ASC NULLS LAST"
            " LIMIT 50"
        ),
    },
    {
        "question": "List pending tasks assigned to Priya",
        "sql": (
            "SELECT t.task_name, t.due_date"
            " FROM tbl_tasks AS t"
            " WHERE t.assigned_to ILIKE '%Priya%'"
            "   AND t.completed_at IS NULL"
            " ORDER BY t.due_date ASC NULLS LAST"
            " LIMIT 50"
        ),
    },
    {
        "question": "How many open tasks does Amit have?",
        "sql": (
            "SELECT COUNT(*) AS open_tasks"
            " FROM tbl_tasks AS t"
            " WHERE t.assigned_to ILIKE '%Amit%'"
            "   AND t.completed_at IS NULL"
        ),
    },
    # ── Tasks by department ─────────────────────────────────────────────────
    {
        "question": "How many pending tasks are in the HR department?",
        "sql": (
            "SELECT COUNT(*) AS pending_count"
            " FROM tbl_tasks AS t"
            " WHERE t.department ILIKE '%HR%'"
            "   AND t.completed_at IS NULL"
        ),
    },
    {
        "question": "Show task counts by department",
        "sql": (
            "SELECT t.department, COUNT(*) AS total_tasks"
            " FROM tbl_tasks AS t"
            " GROUP BY t.department"
            " ORDER BY total_tasks DESC"
            " LIMIT 50"
        ),
    },
    {
        "question": "Show pending task counts by department",
        "sql": (
            "SELECT t.department, COUNT(*) AS pending_tasks"
            " FROM tbl_tasks AS t"
            " WHERE t.completed_at IS NULL"
            " GROUP BY t.department"
            " ORDER BY pending_tasks DESC"
            " LIMIT 50"
        ),
    },
    # ── Due-date queries ────────────────────────────────────────────────────
    {
        "question": "Which tasks are due today?",
        "sql": (
            "SELECT t.task_name, t.assigned_to, t.department"
            " FROM tbl_tasks AS t"
            " WHERE t.due_date = CURRENT_DATE"
            "   AND t.completed_at IS NULL"
            " ORDER BY t.assigned_to"
            " LIMIT 50"
        ),
    },
    {
        "question": "Show tasks due this week",
        "sql": (
            "SELECT t.task_name, t.due_date, t.assigned_to"
            " FROM tbl_tasks AS t"
            " WHERE t.due_date >= CURRENT_DATE"
            "   AND t.due_date < CURRENT_DATE + INTERVAL '7 days'"
            "   AND t.completed_at IS NULL"
            " ORDER BY t.due_date ASC"
            " LIMIT 50"
        ),
    },
    {
        "question": "List overdue tasks",
        "sql": (
            "SELECT t.task_name, t.due_date, t.assigned_to, t.department"
            " FROM tbl_tasks AS t"
            " WHERE t.due_date < CURRENT_DATE"
            "   AND t.completed_at IS NULL"
            " ORDER BY t.due_date ASC"
            " LIMIT 50"
        ),
    },
    # ── Person lookups ──────────────────────────────────────────────────────
    {
        "question": "What is the email of Sunita?",
        "sql": (
            "SELECT DISTINCT e.name, e.email"
            " FROM tbl_employees AS e"
            " WHERE e.name ILIKE '%Sunita%'"
            " LIMIT 10"
        ),
    },
    {
        "question": "What is the phone number of Vikram?",
        "sql": (
            "SELECT DISTINCT e.name, e.phone"
            " FROM tbl_employees AS e"
            " WHERE e.name ILIKE '%Vikram%'"
            " LIMIT 10"
        ),
    },
    # ── Multi-condition queries ─────────────────────────────────────────────
    {
        "question": "List pending tasks for the Finance department",
        "sql": (
            "SELECT t.task_name, t.assigned_to, t.due_date"
            " FROM tbl_tasks AS t"
            " WHERE t.department ILIKE '%Finance%'"
            "   AND t.completed_at IS NULL"
            " ORDER BY t.due_date ASC NULLS LAST"
            " LIMIT 50"
        ),
    },
    {
        "question": "Show all meetings held this month",
        "sql": (
            "SELECT m.meeting_title, m.scheduled_at, m.location"
            " FROM tbl_meetings AS m"
            " WHERE DATE_TRUNC('month', m.scheduled_at) = DATE_TRUNC('month', CURRENT_DATE)"
            " ORDER BY m.scheduled_at DESC"
            " LIMIT 50"
        ),
    },
    # ── Aggregations ────────────────────────────────────────────────────────
    {
        "question": "How many tasks does each person have?",
        "sql": (
            "SELECT t.assigned_to, COUNT(*) AS total_tasks"
            " FROM tbl_tasks AS t"
            " GROUP BY t.assigned_to"
            " ORDER BY total_tasks DESC"
            " LIMIT 50"
        ),
    },
    {
        "question": "Who has the most pending tasks?",
        "sql": (
            "SELECT t.assigned_to, COUNT(*) AS pending_tasks"
            " FROM tbl_tasks AS t"
            " WHERE t.completed_at IS NULL"
            " GROUP BY t.assigned_to"
            " ORDER BY pending_tasks DESC"
            " LIMIT 10"
        ),
    },
    {
        "question": "How many tasks were completed this month?",
        "sql": (
            "SELECT COUNT(*) AS completed_this_month"
            " FROM tbl_tasks AS t"
            " WHERE t.completed_at IS NOT NULL"
            "   AND DATE_TRUNC('month', t.completed_at) = DATE_TRUNC('month', CURRENT_DATE)"
        ),
    },
    {
        "question": "Show the list of all upcoming meetings",
        "sql": (
            "SELECT m.meeting_title, m.scheduled_at, m.location"
            " FROM tbl_meetings AS m"
            " WHERE m.scheduled_at >= NOW()"
            " ORDER BY m.scheduled_at ASC"
            " LIMIT 50"
        ),
    },
]


# ---------------------------------------------------------------------------
# Schema validation helpers
# ---------------------------------------------------------------------------

def _parse_blueprint_tables(blueprint: str) -> dict[str, set[str]]:
    """
    Parse schema_blueprint text into {table_name: {col1, col2, ...}}.
    Blueprint format (from fetch_postgres_schema):
        Table `tbl_tasks` | Rows: ~120
        Columns: task_name (text), assigned_to (text), due_date (date), ...
    """
    tables: dict[str, set[str]] = {}
    current_table: str | None = None

    for line in blueprint.splitlines():
        table_match = re.match(r"Table `([^`]+)`", line)
        if table_match:
            current_table = table_match.group(1).strip()
            tables.setdefault(current_table, set())
            continue

        if current_table and line.startswith("Columns:"):
            cols_part = line[len("Columns:"):].strip()
            for col_entry in cols_part.split(","):
                col_name = col_entry.strip().split(" ")[0].strip()
                if col_name:
                    tables[current_table].add(col_name)

    return tables


def _validate_example(
    example: dict,
    schema_tables: dict[str, set[str]],
) -> list[str]:
    """
    Return a list of validation warnings for this example.
    Checks that every bare identifier that looks like `tbl_<name>` or `tbl_<name> AS alias`
    exists in the schema, and that columns referenced as `alias.col` or `table.col` exist.
    Returns [] if all checks pass.
    """
    if not schema_tables:
        return []  # no blueprint to validate against — skip checks

    warnings: list[str] = []
    sql = example["sql"]

    # Find all table references: FROM/JOIN tbl_xxx (AS alias)?
    table_refs = re.findall(r"\b(tbl_\w+)\b", sql, re.IGNORECASE)
    alias_map: dict[str, str] = {}  # alias -> table_name

    for alias_match in re.finditer(
        r"\b(tbl_\w+)\s+(?:AS\s+)?(\w+)", sql, re.IGNORECASE
    ):
        tbl, alias = alias_match.group(1), alias_match.group(2)
        alias_map[alias.lower()] = tbl.lower()

    known_lower = {t.lower(): t for t in schema_tables}

    for tbl in table_refs:
        if tbl.lower() not in known_lower:
            warnings.append(f"Table '{tbl}' not found in schema (known: {sorted(schema_tables)})")

    # Check qualified column refs: alias.col or table.col
    for col_match in re.finditer(r"\b(\w+)\.(\w+)\b", sql):
        qualifier = col_match.group(1).lower()
        col = col_match.group(2)

        # Resolve qualifier to a table name
        resolved_table = alias_map.get(qualifier) or qualifier
        canonical = known_lower.get(resolved_table)
        if canonical is None:
            continue  # qualifier isn't a known table/alias — skip

        known_cols = schema_tables[canonical]
        if col not in known_cols:
            warnings.append(
                f"Column '{col}' not found in table '{canonical}' "
                f"(known columns: {sorted(known_cols)[:10]}...)"
            )

    return warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _fetch_schema_blueprint(tenant_id: str) -> str | None:
    """Load schema_blueprint directly from the meta DB."""
    try:
        from sqlalchemy import select, text
        from app.database import session_factory
        from app.models import TenantDBCredential
        import uuid as _uuid

        if session_factory is None:
            print("ERROR: DATABASE_URL not configured.", file=sys.stderr)
            return None

        tenant_uuid = _uuid.UUID(tenant_id)
        async with session_factory() as session:
            result = await session.execute(
                select(TenantDBCredential.schema_blueprint).where(
                    TenantDBCredential.tenant_id == tenant_uuid
                )
            )
            blueprint = result.scalar_one_or_none()

        return blueprint
    except Exception as exc:
        print(f"ERROR fetching schema blueprint: {exc}", file=sys.stderr)
        return None


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed few-shot examples for the Minutes of Meeting product."
    )
    parser.add_argument("--tenant-id", required=True, help="UUID of the target tenant.")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually POST to the seed endpoint. Default is dry-run.",
    )
    parser.add_argument(
        "--base-url",
        default=APP_BASE_URL,
        help=f"Base URL of the running app (default: {APP_BASE_URL}).",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    tenant_id = args.tenant_id

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Seeding Minutes of Meeting examples")
    print(f"Tenant:   {tenant_id}")
    print(f"App URL:  {args.base_url}")
    print(f"Examples: {len(SEED_EXAMPLES)}")
    print("─" * 60)

    # ── Fetch live schema for validation ─────────────────────────────────
    print("\nFetching tenant schema blueprint from meta DB...")
    blueprint = await _fetch_schema_blueprint(tenant_id)

    if blueprint:
        schema_tables = _parse_blueprint_tables(blueprint)
        print(f"Found {len(schema_tables)} table(s): {sorted(schema_tables)}\n")
    else:
        schema_tables = {}
        print("WARNING: Could not fetch schema blueprint — skipping column validation.\n")

    # ── Validate and report ───────────────────────────────────────────────
    valid_examples: list[dict] = []
    skipped: list[tuple[int, str, list[str]]] = []

    for idx, example in enumerate(SEED_EXAMPLES):
        warns = _validate_example(example, schema_tables)
        label = f"[{idx+1:02d}] {example['question'][:60]}"
        if warns:
            print(f"  SKIP  {label}")
            for w in warns:
                print(f"        ↳ {w}")
            skipped.append((idx + 1, example["question"], warns))
        else:
            print(f"  OK    {label}")
            valid_examples.append({"question": example["question"], "sql": example["sql"]})

    print(f"\nValid: {len(valid_examples)}  |  Skipped: {len(skipped)}")

    if not valid_examples:
        print("\nNo valid examples to seed. Exiting.")
        sys.exit(1)

    if dry_run:
        print(
            "\n[DRY RUN] No changes made. Review the output above, then re-run with --apply."
        )
        print("\nSQL preview of valid examples:")
        for ex in valid_examples:
            print(f"\n  Q: {ex['question']}")
            print(f"  SQL: {ex['sql']}")
        sys.exit(0)

    # ── POST to seed endpoint ─────────────────────────────────────────────
    if not ADMIN_SECRET_TOKEN:
        print("ERROR: ADMIN_SECRET_TOKEN is not set in environment.", file=sys.stderr)
        sys.exit(1)

    url = f"{args.base_url}/admin/tenant/{tenant_id}/examples/seed"
    headers = {"x-admin-token": ADMIN_SECRET_TOKEN, "Content-Type": "application/json"}
    payload = {"examples": valid_examples}

    print(f"\nPOSTing {len(valid_examples)} examples to {url} ...")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=payload, headers=headers)

    if response.status_code != 200:
        print(f"ERROR: {response.status_code} — {response.text}", file=sys.stderr)
        sys.exit(1)

    result = response.json()
    print(f"\nResult:")
    print(f"  Seeded:  {result.get('seeded', '?')}")
    print(f"  Skipped: {result.get('skipped', '?')}")
    api_errors = result.get("errors", [])
    if api_errors:
        print(f"  API errors ({len(api_errors)}):")
        for err in api_errors:
            print(f"    • {err}")
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
