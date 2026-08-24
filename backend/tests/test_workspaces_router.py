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


def test_list_returns_connections_without_secrets_for_admin(client, monkeypatch):
    monkeypatch.setattr(wr, "is_workspace_admin", lambda **kw: True)

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


def test_list_is_empty_for_non_admin(client, monkeypatch):
    # Non-admins get an empty list (hides the switcher) and never hit the service.
    monkeypatch.setattr(wr, "is_workspace_admin", lambda **kw: False)

    called = {"n": 0}
    async def fake_list():
        called["n"] += 1
        return [{"workspace_id": "ws_1", "name": "P", "host": "h", "client_id": "c",
                 "status": "active", "status_detail": None, "created_by": None,
                 "created_at": None, "updated_at": None}]
    monkeypatch.setattr(wr.ws_service, "list_connections", fake_list)

    resp = client.get("/api/workspaces")
    assert resp.status_code == 200
    assert resp.json() == []
    assert called["n"] == 0


def test_create_requires_admin(client, monkeypatch):
    monkeypatch.setattr(wr, "is_workspace_admin", lambda **kw: False)

    resp = client.post("/api/workspaces", json={
        "name": "Prod", "host": "https://h", "client_id": "cid", "client_secret": "sek",
    })
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["detail"]


def test_create_allowed_for_admin(client, monkeypatch):
    monkeypatch.setattr(wr, "is_workspace_admin", lambda **kw: True)

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


def test_create_maps_value_error_to_400(client, monkeypatch):
    monkeypatch.setattr(wr, "is_workspace_admin", lambda **kw: True)

    async def fake_create(**kwargs):
        raise ValueError("Authentication failed: bad secret")
    monkeypatch.setattr(wr.ws_service, "create_connection", fake_create)

    resp = client.post("/api/workspaces", json={
        "name": "Prod", "host": "https://h", "client_id": "cid", "client_secret": "sek",
    })
    assert resp.status_code == 400
    assert "Authentication failed" in resp.json()["detail"]
