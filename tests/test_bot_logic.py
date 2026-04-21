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
        "Hi! I couldn't find your account. Please contact Botivate support.",
    )


@pytest.mark.asyncio
@respx.mock
async def test_handle_message_calls_mistral_with_system_prompt_containing_customer_and_product(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_active_modules", AsyncMock(return_value=["delivery_tracker"]))
    monkeypatch.setattr(
        bot_logic,
        "get_sql_template",
        AsyncMock(return_value="SELECT customer_name, product_name, delivery_status FROM orders WHERE customer_name ILIKE $1"),
    )
    monkeypatch.setattr(
        bot_logic,
        "classify_intent",
        AsyncMock(
            return_value={
                "module": "delivery_tracker",
                "intent": "delivery_status",
                "entities": {"customer_name": "Rahul Enterprises"},
            }
        ),
    )
    monkeypatch.setattr(
        bot_logic,
        "execute_tenant_query",
        AsyncMock(
            return_value=[
                {
                    "customer_name": "Rahul Enterprises",
                    "product_name": "Solar Panel X200",
                    "delivery_status": "dispatched",
                }
            ]
        ),
    )

    route = respx.post(bot_logic.MISTRAL_CHAT_COMPLETIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Your order is dispatched."}}]},
        )
    )

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="What is my order status?")
    await bot_logic.handle_message(message)

    assert route.called
    request_payload = json.loads(route.calls[0].request.content.decode("utf-8"))
    system_prompt = request_payload["messages"][0]["content"]

    assert "Rahul Enterprises" in system_prompt
    assert "Solar Panel X200" in system_prompt
    send_reply_mock.assert_awaited_once_with(message, "Your order is dispatched.")


@pytest.mark.asyncio
async def test_handle_message_general_intent_bypasses_db_query(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_active_modules", AsyncMock(return_value=["delivery_tracker"]))
    monkeypatch.setattr(bot_logic, "classify_intent", AsyncMock(return_value={"module": "general", "intent": "general_query"}))
    monkeypatch.setattr(bot_logic, "_generate_reply_with_mistral", AsyncMock(return_value="This is a general reply."))
    
    execute_query_mock = AsyncMock()
    monkeypatch.setattr(bot_logic, "execute_tenant_query", execute_query_mock)

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="What can you do?")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, "This is a general reply.")
    execute_query_mock.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_no_sql_template(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_active_modules", AsyncMock(return_value=["delivery_tracker"]))
    monkeypatch.setattr(bot_logic, "classify_intent", AsyncMock(return_value={"module": "delivery_tracker", "intent": "unknown"}))
    monkeypatch.setattr(bot_logic, "get_sql_template", AsyncMock(return_value=None))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Bla bla")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, bot_logic.SQL_TEMPLATE_NOT_FOUND_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_tenant_query_error(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_active_modules", AsyncMock(return_value=["delivery_tracker"]))
    monkeypatch.setattr(bot_logic, "classify_intent", AsyncMock(return_value={"module": "delivery_tracker", "intent": "delivery_status"}))
    monkeypatch.setattr(bot_logic, "get_sql_template", AsyncMock(return_value="SELECT * FROM my_table"))
    monkeypatch.setattr(bot_logic, "execute_tenant_query", AsyncMock(side_effect=QueryExecutionError("DB down")))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, bot_logic.TENANT_QUERY_ERROR_MESSAGE)


@pytest.mark.asyncio
async def test_handle_message_no_results(monkeypatch) -> None:
    tenant = SimpleNamespace(id=uuid.uuid4(), company_name="Demo Corp")
    send_reply_mock = AsyncMock()

    monkeypatch.setattr(bot_logic, "send_reply", send_reply_mock)
    monkeypatch.setattr(bot_logic, "get_tenant_by_chat_id", AsyncMock(return_value=tenant))
    monkeypatch.setattr(bot_logic, "get_active_modules", AsyncMock(return_value=["delivery_tracker"]))
    monkeypatch.setattr(bot_logic, "classify_intent", AsyncMock(return_value={"module": "delivery_tracker", "intent": "delivery_status"}))
    monkeypatch.setattr(bot_logic, "get_sql_template", AsyncMock(return_value="SELECT * FROM my_table"))
    monkeypatch.setattr(bot_logic, "execute_tenant_query", AsyncMock(return_value=[]))

    message = BotMessage(platform=Platform.TELEGRAM, chat_id="123456789", text="Show status")
    await bot_logic.handle_message(message)

    send_reply_mock.assert_awaited_once_with(message, bot_logic.NO_RESULTS_MESSAGE)
