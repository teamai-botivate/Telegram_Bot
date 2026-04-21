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
        "host": "localhost",
        "port": 5432,
        "database_name": "test_db",
        "db_user": "test_user",
        "db_password": "test_password",
        "ssl_required": False
    })
    
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin token."}


def test_schema_map_missing_admin_token() -> None:
    response = client.post("/admin/tenant/schema-map", json={
        "tenant_id": "test-tenant",
        "module": "delivery_tracker",
        "intent": "delivery_status",
        "sql_template": "SELECT * from tests"
    })
    
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin token."}


@pytest.mark.asyncio
async def test_connect_db_success(monkeypatch) -> None:
    monkeypatch.setattr(admin, "ADMIN_SECRET_TOKEN", "secret")
    
    # Mock testing the connection
    mock_connection = AsyncMock()
    mock_connect = AsyncMock(return_value=mock_connection)
    monkeypatch.setattr("asyncpg.connect", mock_connect)
    
    # Mock saving credentials
    mock_save = AsyncMock()
    monkeypatch.setattr(admin, "save_tenant_credentials", mock_save)
    
    # Mock cryptography encrypt since the real one needs a valid FERNET_SECRET_KEY
    monkeypatch.setattr(admin, "encrypt_credential_value", lambda x: f"encrypted_{x}")

    response = client.post(
        "/admin/tenant/connect-db", 
        json={
            "tenant_id": "test-tenant-id",
            "db_type": "postgresql",
            "host": "localhost",
            "port": 5432,
            "database_name": "test_db",
            "db_user": "test_user",
            "db_password": "pwd",
            "ssl_required": False
        },
        headers={"x-admin-token": "secret"}
    )
    
    assert response.status_code == 200
    assert response.json() == {"status": "connected"}
    
    mock_connect.assert_awaited_once()
    mock_save.assert_awaited_once()
    
    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["tenant_id"] == "test-tenant-id"
    assert call_kwargs["db_type"] == "postgresql"
    assert call_kwargs["encrypted_fields"]["host"] == "encrypted_localhost"


@pytest.mark.asyncio
async def test_connect_db_fails_when_db_down(monkeypatch) -> None:
    monkeypatch.setattr(admin, "ADMIN_SECRET_TOKEN", "secret")
    
    # Make the connection fail
    mock_connect = AsyncMock(side_effect=Exception("Connection refused"))
    monkeypatch.setattr("asyncpg.connect", mock_connect)

    response = client.post(
        "/admin/tenant/connect-db", 
        json={
            "tenant_id": "test-tenant-id",
            "db_type": "postgresql",
            "host": "localhost",
            "port": 5432,
            "database_name": "test_db",
            "db_user": "test_user",
            "db_password": "pwd",
            "ssl_required": False
        },
        headers={"x-admin-token": "secret"}
    )
    
    assert response.status_code == 400
    assert "Could not connect" in response.json()["detail"]


@pytest.mark.asyncio
async def test_schema_map_success(monkeypatch) -> None:
    monkeypatch.setattr(admin, "ADMIN_SECRET_TOKEN", "secret")
    
    mock_save = AsyncMock()
    monkeypatch.setattr(admin, "save_schema_map", mock_save)

    response = client.post(
        "/admin/tenant/schema-map", 
        json={
            "tenant_id": "test-tenant-id",
            "module": "delivery_tracker",
            "intent": "delivery_status",
            "sql_template": "SELECT * from tbl"
        },
        headers={"x-admin-token": "secret"}
    )
    
    assert response.status_code == 200
    assert response.json() == {"status": "saved"}
    mock_save.assert_awaited_once_with(
        tenant_id="test-tenant-id",
        module="delivery_tracker",
        intent="delivery_status",
        sql_template="SELECT * from tbl"
    )
