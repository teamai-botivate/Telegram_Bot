from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import webhook


def _build_test_client() -> TestClient:
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app)


def test_get_whatsapp_webhook_returns_challenge_when_verify_token_matches(monkeypatch) -> None:
    monkeypatch.setattr(webhook, "WEBHOOK_VERIFY_TOKEN", "expected-token")
    client = _build_test_client()

    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "expected-token",
            "hub.challenge": 12345,
        },
    )

    assert response.status_code == 200
    assert response.text == "12345"


def test_get_whatsapp_webhook_returns_ok_when_verify_token_is_wrong(monkeypatch) -> None:
    monkeypatch.setattr(webhook, "WEBHOOK_VERIFY_TOKEN", "expected-token")
    client = _build_test_client()

    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": 12345,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_post_whatsapp_webhook_returns_ok_even_without_messages_key() -> None:
    client = _build_test_client()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "status": "delivered",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    response = client.post("/webhook/whatsapp", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
