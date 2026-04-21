import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app import bot_logic
from app.platforms.base import BotMessage, Platform
from app.database import QueryExecutionError


@pytest.mark.asyncio
async def test_handle_message_sends_account_not_found_when_tenant_missing(monkeypatch) -> None:
    send_reply_mock = AsyncMock()
    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=None))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Hello")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(
        message,
        "Hi! I couldn't find your account. Please contact support.",
    )


@pytest.mark.asyncio
@respx.mock
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
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT * FROM orders LIMIT 10"))
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
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT * FROM orders"))
    monkeypatch.setattr(bot_logic, "execute_tenant_query", AsyncMock(side_effect=QueryExecutionError("DB down")))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await bot_logic.handle_message(message)

    # Should send some error message
    send_reply_mock.assert_awaited_once()
    args = send_reply_mock.call_args[0]
    assert "error" in args[1].lower() or "couldn't" in args[1].lower()


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
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=credentials))
    monkeypatch.setattr(bot_logic, "generate_sql_query", AsyncMock(return_value="SELECT * FROM orders"))
    monkeypatch.setattr(bot_logic, "execute_tenant_query", AsyncMock(return_value=[]))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "I couldn't find any data matching your request.")


@pytest.mark.asyncio
async def test_handle_message_no_credentials(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_tenant_credentials", AsyncMock(return_value=None))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Hello")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "Your database connection is not fully configured.")
