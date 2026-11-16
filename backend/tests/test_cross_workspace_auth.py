"""Target-aware client resolution and the admin check for cross-workspace support.

Selecting a remote target redirects the workspace-scoped accessors to the
remote client; the get_home_* accessors always stay home (LLM inference, the
Delta state store, secret scopes, the caller's identity). The narrower agent
override used by Auto-Optimize only affects require_obo_workspace_client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.services import auth


@pytest.fixture
def home_client(monkeypatch) -> MagicMock:
    client = MagicMock(name="home_client")
    client.config.host = "https://home.example.com/"
    monkeypatch.setattr(auth, "_get_default_client", lambda: client)
    return client


@pytest.fixture
def target_client() -> MagicMock:
    client = MagicMock(name="target_client")
    client.config.host = "https://remote.gcp.databricks.com/"
    return client


@pytest.fixture(autouse=True)
def _clear_context():
    auth.clear_obo_user_token()
    auth.clear_target_workspace()
    auth._admin_cache.clear()
    yield
    auth.clear_obo_user_token()
    auth.clear_target_workspace()
    auth._admin_cache.clear()


def test_no_target_uses_home(home_client):
    assert auth.get_target_workspace_id() is None
    assert auth.get_workspace_client() is home_client
    assert auth.get_service_principal_client() is home_client
    assert auth.get_databricks_host() == "https://home.example.com"


def test_target_redirects_workspace_scoped_accessors(home_client, target_client):
    auth.set_target_workspace("ws_remote", target_client, "wh-remote")
    assert auth.get_target_workspace_id() == "ws_remote"
    assert auth.get_target_warehouse_id() == "wh-remote"
    assert auth.get_workspace_client() is target_client
    assert auth.get_service_principal_client() is target_client
    assert auth.require_obo_workspace_client() is target_client
    assert auth.get_databricks_host() == "https://remote.gcp.databricks.com"


def test_home_accessors_ignore_target(home_client, target_client):
    auth.set_target_workspace("ws_remote", target_client)
    assert auth.get_home_workspace_client() is home_client
    assert auth.get_home_service_principal_client() is home_client
    assert auth.get_home_databricks_host() == "https://home.example.com"


def test_llm_api_key_is_home(home_client, target_client):
    home_client.config.token = "home-token"
    target_client.config.token = "remote-token"
    auth.set_target_workspace("ws_remote", target_client)
    assert auth.get_llm_api_key() == "home-token"


def test_clear_target_restores_home(home_client, target_client):
    auth.set_target_workspace("ws_remote", target_client, "wh")
    auth.clear_target_workspace()
    assert auth.get_target_workspace_id() is None
    assert auth.get_target_warehouse_id() is None
    assert auth.get_workspace_client() is home_client


def test_obo_and_target_precedence(home_client, target_client):
    obo = MagicMock(name="obo_client")
    token = auth._obo_client.set(obo)
    try:
        assert auth.get_workspace_client() is obo
        assert auth.get_home_workspace_client() is obo
        assert auth.get_home_service_principal_client() is home_client
        auth.set_target_workspace("ws_remote", target_client)
        assert auth.get_workspace_client() is target_client
        assert auth.get_home_workspace_client() is obo
    finally:
        auth._obo_client.reset(token)


def test_agent_override_only_affects_user_scoped_agent_reads(home_client, target_client):
    obo = MagicMock(name="obo_client")
    obo_token = auth._obo_client.set(obo)
    agent_token = auth.set_agent_client(target_client)
    try:
        assert auth.get_agent_client() is target_client
        assert auth.require_obo_workspace_client() is target_client
        # Data plane stays home.
        assert auth.get_workspace_client() is obo
        assert auth.get_service_principal_client() is home_client
    finally:
        auth.reset_agent_client(agent_token)
        auth._obo_client.reset(obo_token)
    assert auth.get_agent_client() is None


def test_require_obo_without_user_or_target_raises(home_client):
    with pytest.raises(RuntimeError):
        auth.require_obo_workspace_client()


# ── is_workspace_admin ────────────────────────────────────────────────────


def test_admin_via_exact_forwarded_group(monkeypatch):
    monkeypatch.setattr(auth, "is_running_on_databricks_apps", lambda: True)
    monkeypatch.setattr(auth, "_is_admin_via_scim", lambda: pytest.fail("SCIM not needed"))
    assert auth.is_workspace_admin(forwarded_groups="users, admins", forwarded_email="a@x") is True


def test_substring_group_is_not_admin(monkeypatch):
    monkeypatch.setattr(auth, "is_running_on_databricks_apps", lambda: True)
    monkeypatch.setattr(auth, "_is_admin_via_scim", lambda: False)
    assert auth.is_workspace_admin(forwarded_groups="not-admins,admins-lite", forwarded_email="a@x") is False


def test_dev_hatch_only_off_apps(monkeypatch):
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@x")
    monkeypatch.setattr(auth, "is_running_on_databricks_apps", lambda: True)
    monkeypatch.setattr(auth, "_is_admin_via_scim", lambda: False)
    assert auth.is_workspace_admin(forwarded_email="dev@x") is False
    auth._admin_cache.clear()
    monkeypatch.setattr(auth, "is_running_on_databricks_apps", lambda: False)
    assert auth.is_workspace_admin(forwarded_email="dev@x") is True


def test_no_identity_is_never_admin_on_apps(monkeypatch):
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    monkeypatch.delenv("DEV_ADMIN", raising=False)
    monkeypatch.setattr(auth, "is_running_on_databricks_apps", lambda: True)
    monkeypatch.setattr(auth, "_is_admin_via_scim", lambda: pytest.fail("no identity → no SCIM call"))
    assert auth.is_workspace_admin() is False


def test_scim_result_is_cached_per_user(monkeypatch):
    monkeypatch.setattr(auth, "is_running_on_databricks_apps", lambda: True)
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return True

    monkeypatch.setattr(auth, "_is_admin_via_scim", fake)
    assert auth.is_workspace_admin(forwarded_email="u@x") is True
    assert auth.is_workspace_admin(forwarded_email="u@x") is True
    assert calls["n"] == 1


def test_scim_reads_admins_group_from_obo(monkeypatch, home_client):
    obo = MagicMock()
    group = MagicMock()
    group.display = "admins"
    obo.current_user.me.return_value.groups = [group]
    token = auth._obo_client.set(obo)
    try:
        assert auth._is_admin_via_scim() is True
    finally:
        auth._obo_client.reset(token)
    assert auth._is_admin_via_scim() is False  # no OBO → fail closed
