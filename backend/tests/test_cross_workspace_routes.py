"""Routes for cross-workspace connections, storage setup, and the target middleware."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.routers import setup as setup_router
from backend.routers import workspaces as ws_router
from backend.services import app_config, auth, state_store


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DEV_ADMIN", raising=False)
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    auth._admin_cache.clear()
    app_config.clear_active()
    yield
    app_config.clear_active()
    auth.clear_target_workspace()


def _client(router) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── /api/workspaces ───────────────────────────────────────────────────────


def test_workspaces_list_hidden_from_non_admins(monkeypatch):
    monkeypatch.setattr(ws_router, "is_admin_request", lambda r: False)
    monkeypatch.setattr(ws_router.ws_service, "list_connections", lambda: pytest.fail("must not list"))
    assert _client(ws_router.router).get("/api/workspaces").json() == []


def test_workspace_mutations_require_admin(monkeypatch):
    monkeypatch.setattr(ws_router, "is_admin_request", lambda r: False)
    c = _client(ws_router.router)
    assert c.post("/api/workspaces", json={"name": "n", "host": "h", "auth_mode": "app_sp"}).status_code == 403
    assert c.delete("/api/workspaces/ws_1").status_code == 403
    assert c.post("/api/workspaces/ws_1/test").status_code == 403
    assert c.get("/api/workspaces/info").status_code == 403


def test_workspace_create_maps_validation_error_to_400(monkeypatch):
    monkeypatch.setattr(ws_router, "is_admin_request", lambda r: True)

    async def fail(**kw):
        raise ValueError("Authentication failed: 403")

    monkeypatch.setattr(ws_router.ws_service, "create_connection", fail)
    resp = _client(ws_router.router).post(
        "/api/workspaces", json={"name": "n", "host": "h", "auth_mode": "custom_sp", "client_id": "c", "client_secret": "s"}
    )
    assert resp.status_code == 400 and "Authentication failed" in resp.json()["detail"]


def test_workspace_list_never_returns_secret_refs(monkeypatch):
    monkeypatch.setattr(ws_router, "is_admin_request", lambda r: True)

    async def fake_list():
        return [{"workspace_id": "ws_1", "name": "W", "host": "https://h", "auth_mode": "custom_sp",
                 "client_id": "c", "status": "active"}]

    monkeypatch.setattr(ws_router.ws_service, "list_connections", fake_list)
    body = _client(ws_router.router).get("/api/workspaces").json()
    assert body[0]["workspace_id"] == "ws_1"
    assert "secret_key" not in body[0] and "secret_scope" not in body[0]


# ── /api/setup ────────────────────────────────────────────────────────────


def test_setup_status_unconfigured_suggests_gso_catalog(monkeypatch):
    monkeypatch.setenv("GSO_CATALOG", "main")
    monkeypatch.delenv("WORKBENCH_STATE_CATALOG", raising=False)
    monkeypatch.setattr(setup_router, "is_admin_request", lambda r: False)
    body = _client(setup_router.router).get("/api/setup/status").json()
    assert body["configured"] is False and body["is_admin"] is False
    assert body["suggested_catalog"] == "main" and body["suggested_schema"] == "genie_workbench_state"


def test_setup_env_location_counts_as_configured(monkeypatch):
    monkeypatch.setenv("WORKBENCH_STATE_CATALOG", "cat")
    monkeypatch.setenv("WORKBENCH_STATE_SCHEMA", "sch")
    assert app_config.location() == ("cat", "sch") and app_config.source() == "env"
    monkeypatch.setenv("WORKBENCH_STATE_CATALOG", "__WORKBENCH_STATE_CATALOG__")
    assert app_config.location() is None


def test_setup_configure_requires_admin(monkeypatch):
    monkeypatch.setattr(setup_router, "is_admin_request", lambda r: False)
    resp = _client(setup_router.router).post("/api/setup/configure", json={"catalog": "c", "schema": "s"})
    assert resp.status_code == 403


def test_setup_configure_grants_brings_up_storage_and_saves(monkeypatch):
    monkeypatch.setattr(setup_router, "is_admin_request", lambda r: True)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-uuid")
    monkeypatch.setattr(setup_router.delta_store, "warehouse_id", lambda: "wh")
    sql: list[str] = []
    monkeypatch.setattr(setup_router, "_admin_sql", lambda q: sql.append(q) or [])
    reconfigured = {"n": 0}

    async def fake_reconfigure():
        reconfigured["n"] += 1

    monkeypatch.setattr(state_store, "reconfigure", fake_reconfigure)
    saved = {}
    monkeypatch.setattr(setup_router.app_config, "save", lambda c, s, configured_by=None: saved.update(c=c, s=s))

    resp = _client(setup_router.router).post(
        "/api/setup/configure", json={"catalog": "my cat", "schema": "state", "create_schema": True}
    )
    assert resp.status_code == 200, resp.text
    assert sql[0] == "CREATE SCHEMA IF NOT EXISTS `my cat`.`state` COMMENT 'Genie Workbench app state'"
    assert "GRANT USE CATALOG ON CATALOG `my cat` TO `sp-uuid`" in sql
    assert any("ON SCHEMA `my cat`.`state` TO `sp-uuid`" in q for q in sql)
    assert reconfigured["n"] == 1 and saved == {"c": "my cat", "s": "state"}
    assert app_config.location() == ("my cat", "state")


def test_setup_configure_rolls_back_when_table_creation_fails(monkeypatch):
    monkeypatch.setattr(setup_router, "is_admin_request", lambda r: True)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-uuid")
    monkeypatch.delenv("WORKBENCH_STATE_CATALOG", raising=False)
    monkeypatch.setattr(setup_router.delta_store, "warehouse_id", lambda: "wh")
    monkeypatch.setattr(setup_router, "_admin_sql", lambda q: [])

    async def boom():
        raise RuntimeError("PERMISSION_DENIED")

    monkeypatch.setattr(state_store, "reconfigure", boom)
    resp = _client(setup_router.router).post("/api/setup/configure", json={"catalog": "c", "schema": "s"})
    assert resp.status_code == 400
    assert app_config.location() is None


# ── Target middleware ─────────────────────────────────────────────────────


@pytest.fixture
def mw_app(monkeypatch):
    from backend import main

    app = FastAPI()

    @app.get("/api/probe")
    async def probe(request: Request):
        return {
            "target": auth.get_target_workspace_id(),
            "warehouse": auth.get_target_warehouse_id(),
            "state": getattr(request.state, "target_workspace_id", None),
        }

    @app.get("/api/workspaces/probe")
    async def home_probe():
        return {"target": auth.get_target_workspace_id()}

    app.add_middleware(main.OBOAuthMiddleware)
    return TestClient(app)


def test_middleware_without_header_is_home(mw_app):
    assert mw_app.get("/api/probe").json()["target"] is None


def test_middleware_rejects_non_admin_target(mw_app, monkeypatch):
    monkeypatch.setattr(auth, "is_admin_request", lambda r: False)
    resp = mw_app.get("/api/probe", headers={"X-Target-Workspace": "ws_1"})
    assert resp.status_code == 403


def test_middleware_unknown_target_is_an_error_not_home(mw_app, monkeypatch):
    from backend.services import workspaces

    monkeypatch.setattr(auth, "is_admin_request", lambda r: True)

    async def none(_):
        return None

    monkeypatch.setattr(workspaces, "get_client_for_workspace", none)
    resp = mw_app.get("/api/probe", headers={"X-Target-Workspace": "ws_gone"})
    assert resp.status_code == 404


def test_middleware_connection_failure_is_502(mw_app, monkeypatch):
    from backend.services import workspaces

    monkeypatch.setattr(auth, "is_admin_request", lambda r: True)

    async def boom(_):
        raise RuntimeError("token endpoint returned 401")

    monkeypatch.setattr(workspaces, "get_client_for_workspace", boom)
    resp = mw_app.get("/api/probe", headers={"X-Target-Workspace": "ws_1"})
    assert resp.status_code == 502


def test_middleware_sets_target_and_warehouse(mw_app, monkeypatch):
    from backend.services import workspaces

    monkeypatch.setattr(auth, "is_admin_request", lambda r: True)
    remote = MagicMock()

    async def client(_):
        return remote

    async def conn(_):
        return {"workspace_id": "ws_1", "warehouse_id": "wh-remote"}

    monkeypatch.setattr(workspaces, "get_client_for_workspace", client)
    monkeypatch.setattr(workspaces, "get_connection", conn)
    body = mw_app.get("/api/probe", headers={"X-Target-Workspace": "ws_1"}).json()
    assert body == {"target": "ws_1", "warehouse": "wh-remote", "state": "ws_1"}
    # Home-only prefixes never follow the header (and never resolve a client).
    monkeypatch.setattr(workspaces, "get_client_for_workspace", lambda _: pytest.fail("home-only"))
    assert mw_app.get("/api/workspaces/probe", headers={"X-Target-Workspace": "ws_1"}).json() == {"target": None}


# ── Warehouse resolution follows the target ───────────────────────────────


def test_sql_warehouse_follows_target(monkeypatch):
    from backend import sql_executor

    monkeypatch.setenv("SQL_WAREHOUSE_ID", "home-wh")
    assert sql_executor.get_sql_warehouse_id() == "home-wh"
    auth.set_target_workspace("ws_1", MagicMock(), "remote-wh")
    assert sql_executor.get_sql_warehouse_id() == "remote-wh"
    auth.set_target_workspace("ws_2", MagicMock(), None)
    sql_executor._remote_warehouse_cache["ws_2"] = "detected"
    assert sql_executor.get_sql_warehouse_id() == "detected"
