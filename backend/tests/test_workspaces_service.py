"""Unit tests for the cross-workspace connection service.

No real Databricks connectivity — Lakebase, secret-scope, and remote-client
construction are monkeypatched. Focuses on host normalization, secret
stripping, and the create/validate orchestration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.services import workspaces as ws


def test_normalize_host_adds_scheme_and_strips_slash():
    assert ws._normalize_host("dbc-x.cloud.databricks.com/") == "https://dbc-x.cloud.databricks.com"
    assert ws._normalize_host("https://x.databricks.com/") == "https://x.databricks.com"
    assert ws._normalize_host("  http://h/  ") == "http://h"
    assert ws._normalize_host("") == ""


def test_new_workspace_id_format():
    wid = ws._new_workspace_id()
    assert wid.startswith("ws_")
    assert len(wid) == 3 + 16


def test_public_view_strips_secret_material():
    conn = {
        "workspace_id": "ws_abc",
        "name": "Prod",
        "host": "https://h",
        "client_id": "cid",
        "secret_scope": "genie-workbench-workspaces",
        "secret_key": "ws_abc",
        "status": "active",
        "status_detail": "Authenticated",
        "created_by": "me@x.com",
        "created_at": "t0",
        "updated_at": "t1",
    }
    view = ws._public_view(conn)
    assert "secret_scope" not in view
    assert "secret_key" not in view
    assert view["workspace_id"] == "ws_abc"
    assert view["client_id"] == "cid"
    assert view["status"] == "active"


@pytest.mark.asyncio
async def test_list_connections_never_leaks_secrets(monkeypatch):
    async def fake_list():
        return [{
            "workspace_id": "ws_1", "name": "n", "host": "https://h", "client_id": "c",
            "secret_scope": "s", "secret_key": "k", "status": "active",
            "status_detail": None, "created_by": None, "created_at": None, "updated_at": None,
        }]
    monkeypatch.setattr(ws.lakebase, "list_workspace_connections", fake_list)

    out = await ws.list_connections()
    assert len(out) == 1
    assert "secret_key" not in out[0] and "secret_scope" not in out[0]


@pytest.mark.asyncio
async def test_create_connection_rejects_invalid_credentials(monkeypatch):
    # Build a client (mocked) and force validation to fail — nothing should be
    # persisted and no secret should be written.
    monkeypatch.setattr(ws, "build_remote_client", lambda conn, client_secret=None: MagicMock())
    monkeypatch.setattr(ws, "validate_connection", lambda client: {"ok": False, "detail": "bad creds", "user": None, "system_tables": False})

    put_called = {"n": 0}
    monkeypatch.setattr(ws, "_put_secret", lambda *a, **k: put_called.__setitem__("n", put_called["n"] + 1))

    upserts = {"n": 0}
    async def fake_upsert(conn):
        upserts["n"] += 1
    monkeypatch.setattr(ws.lakebase, "upsert_workspace_connection", fake_upsert)

    with pytest.raises(ValueError, match="bad creds"):
        await ws.create_connection(
            name="n", host="https://h", client_id="c", client_secret="s", created_by=None,
        )
    assert put_called["n"] == 0  # secret not stored on validation failure
    assert upserts["n"] == 0     # connection not persisted


@pytest.mark.asyncio
async def test_create_connection_persists_and_strips_on_success(monkeypatch):
    monkeypatch.setattr(ws, "build_remote_client", lambda conn, client_secret=None: MagicMock())
    monkeypatch.setattr(ws, "validate_connection", lambda client: {"ok": True, "detail": "Authenticated", "user": "u@x", "system_tables": True})
    monkeypatch.setattr(ws, "_put_secret", lambda *a, **k: None)

    saved = {}
    async def fake_upsert(conn):
        saved.update(conn)
    monkeypatch.setattr(ws.lakebase, "upsert_workspace_connection", fake_upsert)

    view = await ws.create_connection(
        name="Prod", host="dbc-x.cloud.databricks.com", client_id="cid", client_secret="sek", created_by="me@x",
    )
    # Persisted row carries the secret reference...
    assert saved["secret_scope"] == ws.SECRET_SCOPE
    assert saved["status"] == "active"
    assert saved["host"] == "https://dbc-x.cloud.databricks.com"
    # ...but the returned view does not.
    assert "secret_key" not in view and "secret_scope" not in view
    assert view["name"] == "Prod"


def test_build_remote_client_uses_oauth_m2m(monkeypatch):
    captured = {}

    class FakeConfig:
        # Stand in for databricks.sdk.config.Config so no live host-metadata
        # resolution / network I/O happens during the test.
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeWS:
        def __init__(self, config=None, **kwargs):
            captured["config"] = config

    monkeypatch.setattr(ws, "Config", FakeConfig)
    monkeypatch.setattr(ws, "WorkspaceClient", FakeWS)
    conn = {"workspace_id": "ws_x", "host": "https://h", "client_id": "cid", "secret_key": ""}
    ws.build_remote_client(conn, client_secret="sek")
    cfg = captured["config"]
    assert cfg.auth_type == "oauth-m2m"
    assert cfg.host == "https://h"
    assert cfg.client_id == "cid"
    assert cfg.client_secret == "sek"
