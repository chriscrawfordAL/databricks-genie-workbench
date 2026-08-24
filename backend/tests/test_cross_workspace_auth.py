"""Target-aware client resolution for cross-workspace support.

Verifies that selecting a remote target workspace redirects the workspace-scoped
accessors to the remote client, while the get_home_* accessors always stay on
home (used for LLM/serving, Lakebase, and the caller's identity).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.services import auth


@pytest.fixture
def home_client(monkeypatch) -> MagicMock:
    """Pin the home SP singleton to a mock with a known host."""
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
    yield
    auth.clear_obo_user_token()
    auth.clear_target_workspace()


def test_no_target_uses_home(home_client):
    assert auth.get_target_workspace_id() is None
    assert auth.get_workspace_client() is home_client
    assert auth.get_service_principal_client() is home_client
    assert auth.get_databricks_host() == "https://home.example.com"


def test_target_redirects_workspace_scoped_accessors(home_client, target_client):
    auth.set_target_workspace("ws_remote", target_client)

    assert auth.get_target_workspace_id() == "ws_remote"
    assert auth.get_workspace_client() is target_client
    assert auth.get_service_principal_client() is target_client
    assert auth.require_obo_workspace_client() is target_client
    assert auth.get_databricks_host() == "https://remote.gcp.databricks.com"


def test_home_accessors_ignore_target(home_client, target_client):
    auth.set_target_workspace("ws_remote", target_client)

    # Home accessors must never follow the target (LLM/serving, Lakebase, identity).
    assert auth.get_home_workspace_client() is home_client
    assert auth.get_home_service_principal_client() is home_client
    assert auth.get_home_databricks_host() == "https://home.example.com"


def test_clear_target_restores_home(home_client, target_client):
    auth.set_target_workspace("ws_remote", target_client)
    auth.clear_target_workspace()

    assert auth.get_target_workspace_id() is None
    assert auth.get_workspace_client() is home_client
    assert auth.get_databricks_host() == "https://home.example.com"


def test_obo_takes_precedence_over_home_sp_when_no_target(home_client):
    obo = MagicMock(name="obo_client")
    token = auth._obo_client.set(obo)
    try:
        # With no target, get_workspace_client prefers the OBO (user) client.
        assert auth.get_workspace_client() is obo
        # ...but the home SP accessor is unaffected by OBO.
        assert auth.get_home_service_principal_client() is home_client
    finally:
        auth._obo_client.reset(token)


def test_target_beats_obo(home_client, target_client):
    obo = MagicMock(name="obo_client")
    token = auth._obo_client.set(obo)
    auth.set_target_workspace("ws_remote", target_client)
    try:
        assert auth.get_workspace_client() is target_client
    finally:
        auth._obo_client.reset(token)
