"""Compatibility shim — re-exports from the modularized services package.

Consumers (webhook.py, admin.py) should ideally import directly from
app.services.<module> for explicit dependencies. This shim maintains
backward compatibility during the transition.
"""

# ── Public API re-exports ────────────────────────────────────────────────────
from .services.pipeline import (
    handle_message,
    handle_adddb_callback,
    route_question_to_database,
)
from .services.intent import detect_intent
from .services.llm import is_off_topic
from .services.sqlgen import generate_sql_query, fix_sql, detect_multi_table_query
from .services.format import format_sql_response
from .services.smart_format import smart_format_response
from .services.schema import (
    _validate_generated_sql,
    _extract_table_names_from_blueprint,
    _fix_unsupported_postgres_constructs,
    _has_unsupported_distinct_window,
    _maybe_expand_count_query_across_tables,
    _extract_sheet_value_filters,
)
from .services.context import (
    _build_conversation_context_block,
    _remember_conversation_context,
    _conversation_context,
    MAX_CONVERSATION_CONTEXT_ITEMS,
)
from .services.llm import (
    _get_openai_client,
    _call_openai_formatting,
    _call_openai_sql,
    _call_openai_classifier,
    _call_fast_llm,
)
from .services.core import _openai_client
from .services.pipeline import (
    _summarize_credential_for_router,
    _build_welcome_message,
    _run_postgres_pipeline_for_credential,
    _run_sheets_pipeline_for_credential,
    _handle_unboarded_client,
    _handle_adddb_command,
)
