"""Tests for the /api/workspaces router: admin gating + list shape.

FastAPI TestClient + monkeypatching, no real Databricks. Mirrors the style of
test_auto_optimize_router.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import workspaces as wr


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # Ensure no local DEV admin escape hatch leaks in from the environment.
    monkeypatch.delenv("DEV_ADMIN", raising=False)
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    app = FastAPI()
    app.include_router(wr.router)
    return TestClient(app)


def test_list_returns_connections_without_secrets(client, monkeypatch):
    async def fake_list():
        return [{
            "workspace_id": "ws_1", "name": "Prod", "host": "https://h",
            "client_id": "cid", "status": "active", "status_detail": "Authenticated",
            "created_by": "me@x", "created_at": "t0", "updated_at": "t1",
        }]
    monkeypatch.setattr(wr.ws_service, "list_connections", fake_list)

    resp = client.get("/api/workspaces")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["workspace_id"] == "ws_1"
    assert "secret_key" not in body[0] and "secret_scope" not in body[0]


def test_create_requires_admin(client, monkeypatch):
    monkeypatch.setattr(wr, "_is_admin_via_sdk", lambda: False)

    resp = client.post("/api/workspaces", json={
        "name": "Prod", "host": "https://h", "client_id": "cid", "client_secret": "sek",
    })
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["detail"]


def test_create_allowed_for_admin_via_sdk(client, monkeypatch):
    monkeypatch.setattr(wr, "_is_admin_via_sdk", lambda: True)

    async def fake_create(**kwargs):
        return {
            "workspace_id": "ws_new", "name": kwargs["name"], "host": kwargs["host"],
            "client_id": kwargs["client_id"], "status": "active", "status_detail": "Authenticated",
            "created_by": None, "created_at": None, "updated_at": None,
        }
    monkeypatch.setattr(wr.ws_service, "create_connection", fake_create)

    resp = client.post("/api/workspaces", json={
        "name": "Prod", "host": "https://h", "client_id": "cid", "client_secret": "sek",
    })
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws_new"


def test_create_allowed_via_forwarded_groups_header(client, monkeypatch):
    # Even if the SDK check is unavailable, the forwarded admins group grants access.
    monkeypatch.setattr(wr, "_is_admin_via_sdk", lambda: False)

    async def fake_create(**kwargs):
        return {
            "workspace_id": "ws_h", "name": kwargs["name"], "host": kwargs["host"],
            "client_id": kwargs["client_id"], "status": "active", "status_detail": None,
            "created_by": None, "created_at": None, "updated_at": None,
        }
    monkeypatch.setattr(wr.ws_service, "create_connection", fake_create)

    resp = client.post(
        "/api/workspaces",
        json={"name": "P", "host": "https://h", "client_id": "c", "client_secret": "s"},
        headers={"X-Forwarded-Groups": "users,admins"},
    )
    assert resp.status_code == 200


def test_create_maps_value_error_to_400(client, monkeypatch):
    monkeypatch.setattr(wr, "_is_admin_via_sdk", lambda: True)

    async def fake_create(**kwargs):
        raise ValueError("Authentication failed: bad secret")
    monkeypatch.setattr(wr.ws_service, "create_connection", fake_create)

    resp = client.post("/api/workspaces", json={
        "name": "Prod", "host": "https://h", "client_id": "cid", "client_secret": "sek",
    })
    assert resp.status_code == 400
    assert "Authentication failed" in resp.json()["detail"]
