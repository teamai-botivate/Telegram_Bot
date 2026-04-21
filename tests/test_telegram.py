import json

import httpx
import pytest
import respx

from app.platforms import telegram


@pytest.mark.asyncio
@respx.mock
async def test_send_telegram_message_posts_to_correct_api_url(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", "test-token")

    typing_url = "https://api.telegram.org/bottest-token/sendChatAction"
    message_url = "https://api.telegram.org/bottest-token/sendMessage"

    typing_route = respx.post(typing_url).mock(return_value=httpx.Response(200, json={"ok": True}))
    message_route = respx.post(message_url).mock(return_value=httpx.Response(200, json={"ok": True}))

    await telegram.send_message("123456789", "Hello Telegram")

    assert typing_route.called
    assert message_route.called

    body = json.loads(message_route.calls[0].request.content.decode("utf-8"))
    assert body["chat_id"] == "123456789"
    assert body["text"] == "Hello Telegram"


@pytest.mark.asyncio
@respx.mock
async def test_send_telegram_message_splits_text_longer_than_4096(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", "test-token")

    typing_url = "https://api.telegram.org/bottest-token/sendChatAction"
    message_url = "https://api.telegram.org/bottest-token/sendMessage"

    respx.post(typing_url).mock(return_value=httpx.Response(200, json={"ok": True}))
    message_route = respx.post(message_url).mock(return_value=httpx.Response(200, json={"ok": True}))

    long_text = "x" * 8200
    await telegram.send_message("123456789", long_text)

    assert message_route.call_count == 3

    first_payload = json.loads(message_route.calls[0].request.content.decode("utf-8"))
    second_payload = json.loads(message_route.calls[1].request.content.decode("utf-8"))
    third_payload = json.loads(message_route.calls[2].request.content.decode("utf-8"))

    assert len(first_payload["text"]) == 4096
    assert len(second_payload["text"]) == 4096
    assert len(third_payload["text"]) == 8
