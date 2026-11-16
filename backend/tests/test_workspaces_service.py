"""Cross-workspace connection service: validation, secrets, remote clients.

No network: the OAuth token exchange, secret scope and remote SDK calls are
stubbed. The state store runs in memory.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.services import state_store, workspaces


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    import databricks.sdk.config as sdk_config

    # Never let SDK Config construction reach the network for fake hosts.
    monkeypatch.setattr(
        sdk_config, "get_host_metadata", lambda h: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    monkeypatch.setitem(state_store._memory_store, "workspace_connections", {})
    workspaces._remote_clients.clear()
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-sp-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "app-sp-secret")  # gitleaks:allow
    yield
    workspaces._remote_clients.clear()


@pytest.fixture
def home_sp(monkeypatch):
    sp = MagicMock(name="home_sp")
    store: dict[tuple[str, str], str] = {}

    def put_secret(scope, key, string_value):
        store[(scope, key)] = string_value

    def get_secret(scope, key):
        return SimpleNamespace(value=base64.b64encode(store[(scope, key)].encode()).decode())

    def delete_secret(scope, key):
        store.pop((scope, key), None)

    sp.secrets.put_secret.side_effect = put_secret
    sp.secrets.get_secret.side_effect = get_secret
    sp.secrets.delete_secret.side_effect = delete_secret
    sp._store = store
    monkeypatch.setattr(workspaces, "get_home_service_principal_client", lambda: sp)
    return sp


@pytest.fixture
def token_calls(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    def fake_fetch(host, client_id, secret):
        calls.append((host, client_id, secret))
        if secret == "bad":
            raise RuntimeError("token endpoint returned 401: invalid_client")
        return f"tok-{client_id}", 10**9

    monkeypatch.setattr(workspaces, "fetch_m2m_token", fake_fetch)
    return calls


@pytest.fixture
def remote_probe(monkeypatch):
    """Stub WorkspaceClient construction + the probe calls used by validate_connection."""
    built: list[MagicMock] = []

    def fake_client(*, config, **_):
        c = MagicMock(name="remote_client")
        c.config = config
        c.current_user.me.return_value = SimpleNamespace(user_name="sp@remote", display_name="sp")
        c.get_workspace_id.return_value = 4242
        c.warehouses.list.return_value = [SimpleNamespace(id="wh1", state="RUNNING")]
        c.schemas.list.return_value = iter([SimpleNamespace(name="query")])
        built.append(c)
        return c

    monkeypatch.setattr(workspaces, "WorkspaceClient", fake_client)
    return built


def test_normalize_host():
    assert workspaces.normalize_host("dbc-1.cloud.databricks.com/") == "https://dbc-1.cloud.databricks.com"
    assert workspaces.normalize_host(" https://x.gcp.databricks.com ") == "https://x.gcp.databricks.com"
    assert workspaces.normalize_host("") == ""


def test_public_view_strips_secret_references():
    view = workspaces.public_view({
        "workspace_id": "ws_1", "name": "n", "host": "https://h", "client_id": "c",
        "secret_scope": "scope", "secret_key": "ws_1", "auth_mode": "custom_sp",
    })
    assert "secret_scope" not in view and "secret_key" not in view
    assert view["auth_mode"] == "custom_sp"


async def test_create_custom_sp_validates_then_stores_secret(home_sp, token_calls, remote_probe):
    conn = await workspaces.create_connection(
        name="West", host="remote.gcp.databricks.com", auth_mode="custom_sp",
        client_id="remote-sp", client_secret="s3cret", warehouse_id="  ", created_by="me@x",
    )
    assert conn["status"] == "active"
    assert conn["remote_workspace_id"] == "4242"
    assert conn["warehouse_id"] is None
    assert token_calls[0] == ("https://remote.gcp.databricks.com", "remote-sp", "s3cret")
    stored = state_store._memory_store["workspace_connections"][conn["workspace_id"]]
    assert stored["secret_scope"] == workspaces.SECRET_SCOPE
    assert "client_secret" not in stored and "s3cret" not in str(stored)
    assert home_sp._store[(workspaces.SECRET_SCOPE, conn["workspace_id"])] == "s3cret"


async def test_create_with_bad_credentials_stores_nothing(home_sp, token_calls, remote_probe):
    with pytest.raises(RuntimeError):
        await workspaces.create_connection(
            name="West", host="https://r", auth_mode="custom_sp", client_id="c", client_secret="bad",
        )
    assert state_store._memory_store["workspace_connections"] == {}
    assert home_sp._store == {}


async def test_create_app_sp_uses_own_credentials_and_stores_no_secret(home_sp, token_calls, remote_probe):
    conn = await workspaces.create_connection(name="West", host="https://r", auth_mode="app_sp")
    assert token_calls[0] == ("https://r", "app-sp-id", "app-sp-secret")
    assert conn["auth_mode"] == "app_sp" and conn["client_id"] == "app-sp-id"
    stored = state_store._memory_store["workspace_connections"][conn["workspace_id"]]
    assert stored["secret_key"] is None
    home_sp.secrets.put_secret.assert_not_called()


async def test_create_rejects_duplicate_host(home_sp, token_calls, remote_probe):
    await workspaces.create_connection(name="A", host="https://r", auth_mode="app_sp")
    with pytest.raises(ValueError, match="already exists"):
        await workspaces.create_connection(name="B", host="r", auth_mode="app_sp")


async def test_create_custom_sp_requires_credentials():
    with pytest.raises(ValueError, match="client_id and client_secret"):
        await workspaces.create_connection(name="A", host="https://r", auth_mode="custom_sp")


def test_remote_client_does_not_inherit_home_identity(monkeypatch, home_sp, token_calls):
    monkeypatch.setenv("DATABRICKS_HOST", "https://home.example.com")
    monkeypatch.setenv("DATABRICKS_WORKSPACE_ID", "1111")
    conn = {"workspace_id": "ws_x", "host": "https://remote.example.com", "auth_mode": "app_sp",
            "remote_workspace_id": "4242"}
    client = workspaces.build_remote_client(conn)
    assert client.config.host == "https://remote.example.com"
    assert client.config.workspace_id == "4242"
    assert client.config.client_id is None
    assert client.config.authenticate() == {"Authorization": "Bearer tok-app-sp-id"}


def test_remote_client_cache_and_invalidation(home_sp, token_calls, remote_probe):
    conn = {"workspace_id": "ws_c", "host": "https://r", "auth_mode": "app_sp"}
    a = workspaces.build_remote_client(conn)
    b = workspaces.build_remote_client(conn)
    assert a is b
    workspaces.invalidate_client_cache("ws_c")
    assert workspaces.build_remote_client(conn) is not a


def test_credentials_refresh_before_expiry(monkeypatch):
    issued = iter([("t1", 0.0), ("t2", 10**9)])
    monkeypatch.setattr(workspaces, "fetch_m2m_token", lambda *a: next(issued))
    creds = workspaces.RemoteM2MCredentials("https://r", "c", "s")
    assert creds.token() == "t1"  # expires immediately → next call refreshes
    assert creds.token() == "t2"
    assert creds.token() == "t2"


def test_validate_flags_missing_system_tables_and_warehouses():
    client = MagicMock()
    client.current_user.me.return_value = SimpleNamespace(user_name="sp", display_name=None)
    client.get_workspace_id.return_value = 7
    client.warehouses.list.return_value = []
    client.schemas.list.side_effect = PermissionError("denied")
    result = workspaces.validate_connection(client)
    assert result["ok"] is True
    assert result["system_tables"] is False and result["warehouses"] == 0
    assert "system tables" in result["detail"] and "warehouse" in result["detail"]


def test_validate_auth_failure():
    client = MagicMock()
    client.current_user.me.side_effect = RuntimeError("403 Forbidden\ntrace")
    result = workspaces.validate_connection(client)
    assert result["ok"] is False and result["detail"] == "Authentication failed: 403 Forbidden"


async def test_delete_removes_secret_and_row(home_sp, token_calls, remote_probe):
    conn = await workspaces.create_connection(
        name="West", host="https://r", auth_mode="custom_sp", client_id="c", client_secret="s",
    )
    await workspaces.delete_connection(conn["workspace_id"])
    assert state_store._memory_store["workspace_connections"] == {}
    assert home_sp._store == {}


async def test_test_connection_records_failure_status(home_sp, token_calls, remote_probe, monkeypatch):
    conn = await workspaces.create_connection(name="West", host="https://r", auth_mode="app_sp")
    monkeypatch.setattr(workspaces, "fetch_m2m_token", lambda *a: (_ for _ in ()).throw(RuntimeError("401")))
    result = await workspaces.test_connection(conn["workspace_id"])
    assert result["ok"] is False and result["status"] == "error"
    assert state_store._memory_store["workspace_connections"][conn["workspace_id"]]["status"] == "error"


def test_resolve_remote_warehouse_prefers_configured_then_running():
    client = MagicMock()
    client.warehouses.list.return_value = [
        SimpleNamespace(id="stopped", state=SimpleNamespace(value="STOPPED")),
        SimpleNamespace(id="running", state=SimpleNamespace(value="RUNNING")),
    ]
    assert workspaces.resolve_remote_warehouse({"warehouse_id": "cfg"}, client) == "cfg"
    assert workspaces.resolve_remote_warehouse({}, client) == "running"
    client.warehouses.list.return_value = [SimpleNamespace(id="stopped", state="STOPPED")]
    assert workspaces.resolve_remote_warehouse(None, client) == "stopped"
