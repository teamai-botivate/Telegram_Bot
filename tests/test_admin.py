import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from app.main import app
from app import admin

client = TestClient(app)


def test_connect_db_missing_admin_token() -> None:
    response = client.post("/admin/tenant/connect-db", json={
        "tenant_id": "test-tenant",
        "db_type": "postgresql",
        "connection_url": "postgresql://user:pw@localhost:5432/db",
        "ssl_required": False
    })
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin token."}


def test_create_full_missing_admin_token() -> None:
    response = client.post("/admin/tenant/create-full", json={
        "company_name": "Acme",
        "active_modules": ["general"],
        "db_type": "postgresql",
        "connection_url": "postgresql://user:pw@localhost:5432/db",
        "ssl_required": False
    })
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin token."}


@pytest.mark.asyncio
async def test_connect_db_success(monkeypatch) -> None:
    monkeypatch.setattr(admin, "ADMIN_SECRET_TOKEN", "secret")

    mock_blueprint = "Table `orders` | Columns: id (uuid)"
    mock_fetch_schema = AsyncMock(return_value=mock_blueprint)
    monkeypatch.setattr(admin, "fetch_postgres_schema", mock_fetch_schema)

    mock_save = AsyncMock()
    monkeypatch.setattr(admin, "save_tenant_credentials", mock_save)

    response = client.post(
        "/admin/tenant/connect-db",
        json={
            "tenant_id": "test-tenant-id",
            "db_type": "postgresql",
            "connection_url": "postgresql://user:pw@localhost:5432/db",
            "ssl_required": False
        },
        headers={"x-admin-token": "secret"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "connected"}
    mock_fetch_schema.assert_awaited_once()
    mock_save.assert_awaited_once()

    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["tenant_id"] == "test-tenant-id"
    assert call_kwargs["db_type"] == "postgresql"
    assert call_kwargs["connection_url"] == "postgresql://user:pw@localhost:5432/db"
    assert call_kwargs["schema_blueprint"] == mock_blueprint


@pytest.mark.asyncio
async def test_connect_db_fails_when_db_down(monkeypatch) -> None:
    monkeypatch.setattr(admin, "ADMIN_SECRET_TOKEN", "secret")

    mock_fetch_schema = AsyncMock(side_effect=Exception("Connection refused"))
    monkeypatch.setattr(admin, "fetch_postgres_schema", mock_fetch_schema)

    response = client.post(
        "/admin/tenant/connect-db",
        json={
            "tenant_id": "test-tenant-id",
            "db_type": "postgresql",
            "connection_url": "postgresql://user:pw@localhost:5432/db",
            "ssl_required": False
        },
        headers={"x-admin-token": "secret"}
    )

    assert response.status_code == 400
    assert "failed" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_full_success(monkeypatch) -> None:
    monkeypatch.setattr(admin, "ADMIN_SECRET_TOKEN", "secret")

    mock_fetch_schema = AsyncMock(return_value="Table `orders` | Columns: id (uuid)")
    monkeypatch.setattr(admin, "fetch_postgres_schema", mock_fetch_schema)

    mock_create = AsyncMock(return_value="new-tenant-uuid")
    monkeypatch.setattr(admin, "create_tenant_record", mock_create)

    mock_save = AsyncMock()
    monkeypatch.setattr(admin, "save_tenant_credentials", mock_save)

    response = client.post(
        "/admin/tenant/create-full",
        json={
            "company_name": "Acme Corp",
            "active_modules": ["general"],
            "db_type": "postgresql",
            "connection_url": "postgresql://user:pw@localhost:5432/db",
            "ssl_required": False
        },
        headers={"x-admin-token": "secret"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "created"
    assert "tenant_id" in data
