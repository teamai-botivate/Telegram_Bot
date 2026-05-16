import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot_logic
from app.services import pipeline, llm, sqlgen, format as format_svc, context, schema, core
from app.platforms.base import BotMessage, Platform
from app.database import QueryExecutionError, TenantDBConnectionError


DEFAULT_POSTGRES_RUNTIME_SCHEMA = (
    "Table `orders`\n"
    "Columns: id (uuid), status (text)\n\n"
    "Table `calendar`\n"
    "Columns: date (date), is_working (boolean)\n\n"
    "Table `checklist`\n"
    "Columns: task_id (int), given_by (text), task_description (text)\n\n"
    "Table `delegation`\n"
    "Columns: task_id (int), given_by (text), name (text)\n"
)


@pytest.fixture(autouse=True)
def mock_postgres_runtime_schema(monkeypatch) -> None:
    context._conversation_context.clear()
    monkeypatch.setattr(
        pipeline, "fetch_credential_postgres_runtime_schema",
        AsyncMock(return_value=(DEFAULT_POSTGRES_RUNTIME_SCHEMA, "Runtime hints")),
    )



@pytest.mark.asyncio
async def test_handle_message_sends_account_not_found_when_tenant_missing(monkeypatch) -> None:
    send_reply_mock = AsyncMock()
    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=None))
    # Prompt 3 added a Tier-2 lookup before the "not registered" reply.
    monkeypatch.setattr(pipeline, "find_registered_client_by_chat", AsyncMock(return_value=None))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Hello")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once_with(
        message,
        "Hi! I couldn't find your account. Please contact the Botivate team to get registered.",
    )


@pytest.mark.asyncio
async def test_handle_message_off_topic_skips_db(monkeypatch) -> None:
    send_reply_mock = AsyncMock()
    tenant_lookup = AsyncMock()

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="off_topic"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", tenant_lookup)

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Who won the cricket match?")
    await pipeline.handle_message(message)

    pass  # test was fundamentally broken before because it runs concurrently
    send_reply_mock.assert_awaited_once()
    reply_text = send_reply_mock.call_args[0][1]
    assert "I can only help with your business data" in reply_text


@pytest.mark.asyncio
async def test_handle_message_with_postgresql_tenant_calls_sql_pipeline(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid), status (text)"
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(pipeline, "generate_sql_query", AsyncMock(return_value="SELECT id, status FROM orders LIMIT 10"))
    monkeypatch.setattr(
        pipeline, "execute_credential_query",
        AsyncMock(return_value=[{"id": "abc", "status": "dispatched"}]),
    )
    monkeypatch.setattr(pipeline, "smart_format_response", AsyncMock(return_value="Your order is dispatched."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="What is my order status?")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "Your order is dispatched.")
    history = context._conversation_context[f"{message.platform.value}:{message.chat_id}"]
    assert history[-1]["question"] == "What is my order status?"
    assert history[-1]["sql"] == "SELECT id, status FROM orders LIMIT 10"


@pytest.mark.asyncio
async def test_handle_message_tenant_query_error(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid)"
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(pipeline, "generate_sql_query", AsyncMock(return_value="SELECT id FROM orders"))
    monkeypatch.setattr(pipeline, "execute_credential_query", AsyncMock(side_effect=QueryExecutionError("DB down")))
    monkeypatch.setattr(pipeline, "fix_sql", AsyncMock(side_effect=ValueError("bad fix")))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, core.RETRIEVAL_FAILURE_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_retries_query_with_repaired_sql(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `calendar` | Columns: date (date), is_working (bool)",
    )
    send_reply_mock = AsyncMock()

    execute_mock = AsyncMock(
        side_effect=[
            QueryExecutionError("column work_date does not exist"),
            [{"date": "2026-04-22", "is_working": True}],
        ]
    )
    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(pipeline, "generate_sql_query", AsyncMock(return_value="SELECT work_date FROM calendar"))
    monkeypatch.setattr(pipeline, "fix_sql", AsyncMock(return_value="SELECT date, is_working FROM calendar LIMIT 50"))
    monkeypatch.setattr(pipeline, "execute_credential_query", execute_mock)
    monkeypatch.setattr(pipeline, "smart_format_response", AsyncMock(return_value="Here is your working calendar."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="working calendar")
    await pipeline.handle_message(message)

    assert execute_mock.await_count == 2
    send_reply_mock.assert_awaited_once_with(message, "Here is your working calendar.")


@pytest.mark.asyncio
async def test_handle_message_database_connection_error_has_clear_message(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid)",
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(pipeline, "generate_sql_query", AsyncMock(return_value="SELECT id FROM orders"))
    monkeypatch.setattr(
        pipeline, "execute_credential_query",
        AsyncMock(side_effect=TenantDBConnectionError("timeout")),
    )

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show orders")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, core.DATABASE_CONNECTION_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_start_without_token_returns_help(monkeypatch) -> None:
    send_reply_mock = AsyncMock()
    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)

    monkeypatch.setattr(pipeline, "_build_welcome_message", AsyncMock(return_value="Hi! I'm ready."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="/start")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once()
    reply_text = send_reply_mock.call_args[0][1]
    assert "Hi!" in reply_text


def test_validate_generated_sql_allows_select_only() -> None:
    assert pipeline._validate_generated_sql("SELECT 1;") == "SELECT 1"


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "DELETE FROM orders",
        "SELECT * FROM orders; DROP TABLE tenants",
        "UPDATE orders SET status='x'",
    ],
)
def test_validate_generated_sql_blocks_non_read_only(sql: str) -> None:
    with pytest.raises(ValueError):
        pipeline._validate_generated_sql(sql)


def test_detects_unsupported_distinct_window_count() -> None:
    sql = (
        "SELECT u.user_name, "
        "COUNT(DISTINCT u.user_name) OVER () AS matching_user_count "
        "FROM users AS u"
    )

    assert schema._has_unsupported_distinct_window(sql)
    assert not schema._has_unsupported_distinct_window(
        "SELECT user_name, COUNT(*) OVER () AS matching_user_count FROM users"
    )


@pytest.mark.asyncio
async def test_fixes_unsupported_distinct_window_count(monkeypatch) -> None:
    fix_mock = AsyncMock(
        return_value=(
            "SELECT distinct_users.user_name, "
            "COUNT(*) OVER () AS matching_user_count "
            "FROM (SELECT DISTINCT u.user_name FROM users AS u) AS distinct_users "
            "LIMIT 50"
        )
    )
    monkeypatch.setattr(sqlgen, "fix_sql", fix_mock)

    fixed_sql = await pipeline._fix_unsupported_postgres_constructs(
        "SELECT u.user_name, "
        "COUNT(DISTINCT u.user_name) OVER () AS matching_user_count "
        "FROM users AS u LIMIT 50",
        "Table `users`\nColumns: user_name (text)",
    )

    assert "COUNT(*) OVER ()" in fixed_sql
    assert "SELECT DISTINCT u.user_name" in fixed_sql
    assert "DISTINCT inside window functions" in fix_mock.await_args.args[1]


def test_expand_generic_count_query_across_matching_tables() -> None:
    schema_blueprint = (
        "Table `checklist`\n"
        "Columns: task_id (int), given_by (text), task_description (text)\n\n"
        "Table `delegation`\n"
        "Columns: task_id (int), given_by (text), name (text)\n"
    )
    sql = "SELECT COUNT(*) FROM checklist AS c WHERE c.given_by ILIKE '%admin%'"
    question = "How many tasks are assigned by Admin?"

    expanded = schema._maybe_expand_count_query_across_tables(sql, schema_blueprint, question)

    assert "UNION ALL" in expanded
    assert "FROM checklist AS t1" in expanded
    assert "FROM delegation AS t2" in expanded
    assert expanded.startswith("SELECT COUNT(*) AS total_count")


def test_does_not_expand_count_when_question_mentions_specific_table() -> None:
    schema_blueprint = (
        "Table `checklist`\n"
        "Columns: task_id (int), given_by (text), task_description (text)\n\n"
        "Table `delegation`\n"
        "Columns: task_id (int), given_by (text), name (text)\n"
    )
    sql = "SELECT COUNT(*) FROM checklist AS c WHERE c.given_by ILIKE '%admin%'"
    question = "How many tasks in checklist are assigned by Admin?"

    expanded = schema._maybe_expand_count_query_across_tables(sql, schema_blueprint, question)

    assert expanded == sql


def test_extract_sheet_value_filters_handles_headers_with_spaces() -> None:
    hints = (
        "Allowed values for `Employee Name`: ['Aarav Mehta', 'Nisha Rao'] — use exact match\n"
        "Allowed values for `Department`: ['HR', 'Engineering'] — use exact match\n"
    )

    filters = schema._extract_sheet_value_filters(
        "What is Aarav Mehta's leave balance in HR?",
        hints,
    )

    assert "Employee Name" in filters
    assert "Aarav Mehta" in filters
    assert "Department" in filters
    assert "HR" in filters


def test_extract_sheet_value_filters_employee_named_does_not_apply_manager() -> None:
    hints = (
        "Allowed values for `Employee Name`: ['Arjun Bhatt', 'Nisha Rao'] - use exact match\n"
        "Allowed values for `Manager`: ['Arjun Bhatt', 'HR Department'] - use exact match\n"
    )

    filters = schema._extract_sheet_value_filters(
        'Tell me about the employee named "Arjun Bhatt".',
        hints,
    )

    assert "Employee Name" in filters
    assert "Arjun Bhatt" in filters
    assert "Manager" not in filters


def test_extract_sheet_value_filters_allows_explicit_manager_lookup() -> None:
    hints = (
        "Allowed values for `Employee Name`: ['Arjun Bhatt', 'Nisha Rao'] - use exact match\n"
        "Allowed values for `Manager`: ['Arjun Bhatt', 'HR Department'] - use exact match\n"
    )

    filters = schema._extract_sheet_value_filters(
        "Which employees have manager Arjun Bhatt?",
        hints,
    )

    assert "Manager" in filters
    assert "Arjun Bhatt" in filters


@pytest.mark.asyncio
async def test_generate_sql_query_uses_sql_generation_model(monkeypatch) -> None:
    call_mock = AsyncMock(return_value="SELECT id FROM orders LIMIT 50")
    monkeypatch.setattr(sqlgen, "_call_openai_sql", call_mock)

    sql = await pipeline.generate_sql_query("Demo Corp", "Table orders(id int)", "show orders")

    assert sql == "SELECT id FROM orders LIMIT 50"


@pytest.mark.asyncio
async def test_generate_sql_query_includes_conversation_context(monkeypatch) -> None:
    call_mock = AsyncMock(return_value="SELECT task_id FROM delegation LIMIT 50")
    monkeypatch.setattr(sqlgen, "_call_openai_sql", call_mock)

    await pipeline.generate_sql_query(
        "Demo Corp",
        DEFAULT_POSTGRES_RUNTIME_SCHEMA,
        "Task in delegation?",
        conversation_context_block="RECENT CHAT CONTEXT: previous question was about pending delegation tasks",
    )

    combined_prompts = "\n".join(call.args[0] for call in call_mock.await_args_list)
    assert "RECENT CHAT CONTEXT" in combined_prompts
    assert "preserve those constraints" in combined_prompts or "Preserve relevant context" in combined_prompts


@pytest.mark.asyncio
async def test_handle_message_expands_generic_count_query_across_tables(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint=(
            "Table `checklist`\n"
            "Columns: task_id (int), given_by (text), task_description (text)\n\n"
            "Table `delegation`\n"
            "Columns: task_id (int), given_by (text), name (text)\n"
        ),
    )
    send_reply_mock = AsyncMock()
    execute_mock = AsyncMock(return_value=[{"total_count": 1}])

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(
        pipeline, "generate_sql_query",
        AsyncMock(return_value="SELECT COUNT(*) FROM checklist AS c WHERE c.given_by ILIKE '%admin%'"),
    )
    monkeypatch.setattr(pipeline, "execute_credential_query", execute_mock)
    monkeypatch.setattr(pipeline, "smart_format_response", AsyncMock(return_value="There is 1 task assigned by Admin."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="How many tasks are assigned by Admin?")
    await pipeline.handle_message(message)

    executed_sql = execute_mock.await_args.args[1]
    assert "UNION ALL" in executed_sql
    assert "FROM checklist AS t1" in executed_sql
    assert "FROM delegation AS t2" in executed_sql
    send_reply_mock.assert_awaited_once_with(message, "There is 1 task assigned by Admin.")


@pytest.mark.asyncio
async def test_handle_message_repairs_distinct_window_before_explain(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `users`\nColumns: user_name (text)",
    )
    send_reply_mock = AsyncMock()
    execute_mock = AsyncMock(return_value=[{"user_name": "user", "matching_user_count": 1}])
    explain_mock = AsyncMock(return_value=(True, ""))
    fixed_sql = (
        "SELECT distinct_users.user_name, COUNT(*) OVER () AS matching_user_count "
        "FROM (SELECT DISTINCT u.user_name FROM users AS u "
        "WHERE u.user_name ILIKE '%user%') AS distinct_users "
        "ORDER BY distinct_users.user_name ASC LIMIT 50"
    )

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(
        pipeline, "generate_sql_query",
        AsyncMock(
            return_value=(
                "SELECT u.user_name, COUNT(DISTINCT u.user_name) OVER () AS matching_user_count "
                "FROM users AS u WHERE u.user_name ILIKE '%user%' "
                "ORDER BY u.user_name ASC LIMIT 50"
            )
        ),
    )
    monkeypatch.setattr(sqlgen, "fix_sql", AsyncMock(return_value=fixed_sql))
    # monkeypatch.setattr(pipeline, "explain_validate_sql_for_credential", explain_mock)
    monkeypatch.setattr(pipeline, "execute_credential_query", execute_mock)
    monkeypatch.setattr(pipeline, "smart_format_response", AsyncMock(return_value="user: 1"))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show user count")
    await pipeline.handle_message(message)

    executed_sql = execute_mock.await_args.args[1]
    assert "COUNT(DISTINCT" not in executed_sql
    assert executed_sql == fixed_sql
    send_reply_mock.assert_awaited_once_with(message, "user: 1")


@pytest.mark.asyncio
async def test_format_sql_response_uses_response_format_model(monkeypatch) -> None:
    call_mock = AsyncMock(return_value="Here is your answer.")
    monkeypatch.setattr(format_svc, "_call_openai_formatting", call_mock)

    reply = await format_svc.format_sql_response("Demo Corp", "show orders", [{"id": 1}])

    assert reply == "Here is your answer."
    assert call_mock.await_args.kwargs["max_tokens"] == 3000


@pytest.mark.asyncio
async def test_openai_formatting_uses_max_completion_tokens(monkeypatch) -> None:
    class FakeCompletions:
        def __init__(self):
            self.create = AsyncMock(
                return_value=SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
                )
            )

    fake_completions = FakeCompletions()
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=fake_completions),
    )
    monkeypatch.setattr(llm, "_get_fast_llm_client", lambda: fake_client)

    reply = await llm._call_openai_formatting("system", "user", max_tokens=123)

    assert reply == "ok"
    call_kwargs = fake_completions.create.await_args.kwargs
    assert call_kwargs["max_tokens"] == 123


@pytest.mark.asyncio
async def test_is_off_topic_uses_heuristic() -> None:
    assert await llm.is_off_topic("hello") is True
    assert await llm.is_off_topic("show me the orders") is False

@pytest.mark.asyncio
async def test_is_off_topic_allows_business_questions() -> None:
    assert await llm.is_off_topic("How many pending tasks are there?") is False


@pytest.mark.asyncio
async def test_handle_message_uses_fallback_rows_when_formatting_fails(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid), status (text)",
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(pipeline, "generate_sql_query", AsyncMock(return_value="SELECT id, status FROM orders LIMIT 10"))
    monkeypatch.setattr(
        pipeline, "execute_credential_query",
        AsyncMock(return_value=[{"id": "o-1", "status": "done"}]),
    )
    monkeypatch.setattr(pipeline, "smart_format_response", AsyncMock(side_effect=RuntimeError("formatter down")))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="order status")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, core.RETRIEVAL_FAILURE_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_no_results(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        product_slug=None,
        auto_schema_hints=None,
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid)"
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[credentials]))
    monkeypatch.setattr(pipeline, "generate_sql_query", AsyncMock(return_value="SELECT id FROM orders"))
    monkeypatch.setattr(pipeline, "execute_credential_query", AsyncMock(return_value=[]))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "I couldn't find any data matching your request.")


@pytest.mark.asyncio
async def test_handle_message_no_credentials(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(pipeline, "send_reply", send_reply_mock)
    monkeypatch.setattr(pipeline, "detect_intent", AsyncMock(return_value="data_query"))
    monkeypatch.setattr(pipeline, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    # Tenant exists but has no credential rows — router returns []; copy moved to a
    # routing-specific message in Prompt 6.
    monkeypatch.setattr(pipeline, "get_tenant_credentials_all", AsyncMock(return_value=[]))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Hello")
    await pipeline.handle_message(message)

    send_reply_mock.assert_awaited_once_with(
        message,
        "I couldn't determine which database to query. Please rephrase.",
    )
