import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot_logic
from app.platforms.base import BotMessage, Platform
from app.database import QueryExecutionError, TenantDBConnectionError


@pytest.mark.asyncio
async def test_handle_message_sends_account_not_found_when_tenant_missing(monkeypatch) -> None:
    send_reply_mock = AsyncMock()
    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=None))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Hello")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(
        message,
        "Hi! I couldn't find your account. Please contact support.",
    )


@pytest.mark.asyncio
async def test_handle_message_off_topic_skips_db(monkeypatch) -> None:
    send_reply_mock = AsyncMock()
    tenant_lookup = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=True))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", tenant_lookup)

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Who won the cricket match?")
    await bot_logic.handle_message(message)

    tenant_lookup.assert_not_awaited()
    send_reply_mock.assert_awaited_once()
    reply_text = send_reply_mock.call_args[0][1]
    assert "I can only help with your business data" in reply_text


@pytest.mark.asyncio
async def test_handle_message_with_postgresql_tenant_calls_mistral(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid), status (text)"
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT id, status FROM orders LIMIT 10"))
    monkeypatch.setattr(
        bot_logic,
        "execute_tenant_query",
        AsyncMock(return_value=[{"id": "abc", "status": "dispatched"}]),
    )
    monkeypatch.setattr(bot_logic, "format_sql_response", AsyncMock(return_value="Your order is dispatched."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="What is my order status?")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "Your order is dispatched.")


@pytest.mark.asyncio
async def test_handle_message_tenant_query_error(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid)"
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT id FROM orders"))
    monkeypatch.setattr(bot_logic, "execute_tenant_query", AsyncMock(side_effect=QueryExecutionError("DB down")))
    monkeypatch.setattr(bot_logic, "fix_sql", AsyncMock(side_effect=ValueError("bad fix")))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, bot_logic.RETRIEVAL_FAILURE_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_retries_query_with_repaired_sql(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
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
    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT work_date FROM calendar"))
    monkeypatch.setattr(bot_logic, "fix_sql", AsyncMock(return_value="SELECT date, is_working FROM calendar LIMIT 50"))
    monkeypatch.setattr(bot_logic, "execute_tenant_query", execute_mock)
    monkeypatch.setattr(bot_logic, "format_sql_response", AsyncMock(return_value="Here is your working calendar."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="working calendar")
    await bot_logic.handle_message(message)

    assert execute_mock.await_count == 2
    send_reply_mock.assert_awaited_once_with(message, "Here is your working calendar.")


@pytest.mark.asyncio
async def test_handle_message_database_connection_error_has_clear_message(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid)",
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT id FROM orders"))
    monkeypatch.setattr(
        bot_logic,
        "execute_tenant_query",
        AsyncMock(side_effect=TenantDBConnectionError("timeout")),
    )

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show orders")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, bot_logic.DATABASE_CONNECTION_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_start_without_token_returns_help(monkeypatch) -> None:
    send_reply_mock = AsyncMock()
    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)

    monkeypatch.setattr(bot_logic, "_build_welcome_message", AsyncMock(return_value="Hi! I'm ready."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="/start")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once()
    reply_text = send_reply_mock.call_args[0][1]
    assert "Hi!" in reply_text


def test_validate_generated_sql_allows_select_only() -> None:
    assert bot_logic._validate_generated_sql("SELECT 1;") == "SELECT 1"


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "DELETE FROM orders",
        "SELECT * FROM orders; DROP TABLE tenants",
        "UPDATE orders SET status='x'",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "SELECT * FROM orders",
    ],
)
def test_validate_generated_sql_blocks_non_read_only(sql: str) -> None:
    with pytest.raises(ValueError):
        bot_logic._validate_generated_sql(sql)


def test_expand_generic_count_query_across_matching_tables() -> None:
    schema_blueprint = (
        "Table `checklist`\n"
        "Columns: task_id (int), given_by (text), task_description (text)\n\n"
        "Table `delegation`\n"
        "Columns: task_id (int), given_by (text), name (text)\n"
    )
    sql = "SELECT COUNT(*) FROM checklist AS c WHERE c.given_by ILIKE '%admin%'"
    question = "How many tasks are assigned by Admin?"

    expanded = bot_logic._maybe_expand_count_query_across_tables(sql, schema_blueprint, question)

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

    expanded = bot_logic._maybe_expand_count_query_across_tables(sql, schema_blueprint, question)

    assert expanded == sql


def test_extract_sheet_value_filters_handles_headers_with_spaces() -> None:
    hints = (
        "Allowed values for `Employee Name`: ['Aarav Mehta', 'Nisha Rao'] — use exact match\n"
        "Allowed values for `Department`: ['HR', 'Engineering'] — use exact match\n"
    )

    filters = bot_logic._extract_sheet_value_filters(
        "What is Aarav Mehta's leave balance in HR?",
        hints,
    )

    assert "Employee Name" in filters
    assert "Aarav Mehta" in filters
    assert "Department" in filters
    assert "HR" in filters


@pytest.mark.asyncio
async def test_generate_sql_query_uses_sql_generation_model(monkeypatch) -> None:
    call_mock = AsyncMock(return_value="SELECT id FROM orders LIMIT 50")
    monkeypatch.setattr(bot_logic, "_call_openai_sql", call_mock)

    sql = await bot_logic.generate_sql_query("Demo Corp", "Table orders(id int)", "show orders")

    assert sql == "SELECT id FROM orders LIMIT 50"


@pytest.mark.asyncio
async def test_handle_message_expands_generic_count_query_across_tables(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
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

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(
        bot_logic,
        "generate_sql_query",
        AsyncMock(return_value="SELECT COUNT(*) FROM checklist AS c WHERE c.given_by ILIKE '%admin%'"),
    )
    monkeypatch.setattr(bot_logic, "execute_tenant_query", execute_mock)
    monkeypatch.setattr(bot_logic, "format_sql_response", AsyncMock(return_value="There is 1 task assigned by Admin."))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="How many tasks are assigned by Admin?")
    await bot_logic.handle_message(message)

    executed_sql = execute_mock.await_args.args[1]
    assert "UNION ALL" in executed_sql
    assert "FROM checklist AS t1" in executed_sql
    assert "FROM delegation AS t2" in executed_sql
    send_reply_mock.assert_awaited_once_with(message, "There is 1 task assigned by Admin.")


@pytest.mark.asyncio
async def test_format_sql_response_uses_response_format_model(monkeypatch) -> None:
    call_mock = AsyncMock(return_value="Here is your answer.")
    monkeypatch.setattr(bot_logic, "_call_mistral", call_mock)
    monkeypatch.setattr(bot_logic, "RESPONSE_FORMAT_MODEL", "mistral-small-latest")

    reply = await bot_logic.format_sql_response("Demo Corp", "show orders", [{"id": 1}])

    assert reply == "Here is your answer."
    assert call_mock.await_args.kwargs["model"] == "mistral-small-latest"


@pytest.mark.asyncio
async def test_handle_message_uses_fallback_rows_when_formatting_fails(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid), status (text)",
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT id, status FROM orders LIMIT 10"))
    monkeypatch.setattr(
        bot_logic,
        "execute_tenant_query",
        AsyncMock(return_value=[{"id": "o-1", "status": "done"}]),
    )
    monkeypatch.setattr(bot_logic, "format_sql_response", AsyncMock(side_effect=RuntimeError("formatter down")))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="order status")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, bot_logic.RETRIEVAL_FAILURE_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_no_results(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    credentials = SimpleNamespace(
        db_type="postgresql",
        connection_url="encrypted_url",
        schema_blueprint="Table `orders` | Columns: id (uuid)"
    )
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT id FROM orders"))
    monkeypatch.setattr(bot_logic, "execute_tenant_query", AsyncMock(return_value=[]))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "I couldn't find any data matching your request.")


@pytest.mark.asyncio
async def test_handle_message_no_credentials(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "is_off_topic", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=None))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Hello")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "Your database connection is not fully configured.")
