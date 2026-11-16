"""Cross-workspace connections (remote workspaces in the same account).

The app is deployed in one *home* workspace but can list, scan, create,
monitor and optimize Genie Agents in *remote* workspaces of the same
Databricks account. Each remote workspace is reached with service-principal
OAuth (M2M) credentials, in one of two modes:

* ``app_sp`` — the app's **own** service principal. App SPs are account-level
  identities, so an admin can add this SP to another workspace; the app then
  exchanges its own client id/secret at the remote workspace's OIDC endpoint.
  No extra secret is stored.
* ``custom_sp`` — a separate SP registered in the remote workspace. Its client
  secret is stored in a Databricks **secret scope in the home workspace**; the
  state store keeps only the ``(secret_scope, secret_key)`` reference.

Remote clients authenticate with a plain OAuth client-credentials exchange
against ``<remote-host>/oidc/v1/token`` (cloud-agnostic; avoids SDK credential
chains that pick up the home workspace's identity) and refresh the token
before it expires. Clients are cached per connection.

All secret-scope operations use the HOME service-principal client so they
never follow the selected target workspace.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
import uuid

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config
from databricks.sdk.credentials_provider import CredentialsStrategy

from backend._telemetry import PRODUCT_NAME, PRODUCT_VERSION
from backend.services import state_store
from backend.services.auth import get_home_service_principal_client

logger = logging.getLogger(__name__)

# Databricks secret scope (home workspace) holding custom-SP client secrets.
SECRET_SCOPE = os.environ.get("WORKSPACES_SECRET_SCOPE", "genie-workbench-workspaces")

AUTH_APP_SP = "app_sp"
AUTH_CUSTOM_SP = "custom_sp"

_TOKEN_REFRESH_MARGIN_SECONDS = 300
_CLIENT_TTL_SECONDS = 600  # rebuild cached clients so rotated secrets are picked up

_remote_clients: dict[str, tuple[float, WorkspaceClient]] = {}


# ── Helpers ──────────────────────────────────────────────────────────────


def normalize_host(host: str) -> str:
    """``https://<host>`` with no trailing slash (or "" for empty input)."""
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


def _new_connection_id() -> str:
    return f"ws_{uuid.uuid4().hex[:16]}"


def _app_sp_credentials() -> tuple[str, str]:
    client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    if not (client_id and secret):
        raise ValueError(
            "The app's own service-principal credentials are not available "
            "(DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET). Use a custom service principal."
        )
    return client_id, secret


def app_sp_client_id() -> str:
    return os.environ.get("DATABRICKS_CLIENT_ID", "").strip()


# ── Secret storage (home workspace) ──────────────────────────────────────


def _ensure_scope() -> None:
    client = get_home_service_principal_client()
    try:
        client.secrets.create_scope(scope=SECRET_SCOPE)
        logger.info("Created secret scope '%s'", SECRET_SCOPE)
    except Exception as e:  # noqa: BLE001
        if "ALREADY_EXISTS" not in str(e).upper():
            raise


def _put_secret(secret_key: str, client_secret: str) -> None:
    _ensure_scope()
    get_home_service_principal_client().secrets.put_secret(
        scope=SECRET_SCOPE, key=secret_key, string_value=client_secret
    )


def _read_secret(secret_scope: str, secret_key: str) -> str:
    resp = get_home_service_principal_client().secrets.get_secret(scope=secret_scope, key=secret_key)
    # The Secrets API returns the value base64-encoded.
    return base64.b64decode(resp.value or "").decode("utf-8")


def _delete_secret(secret_scope: str, secret_key: str) -> None:
    try:
        get_home_service_principal_client().secrets.delete_secret(scope=secret_scope, key=secret_key)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to delete secret %s/%s: %s", secret_scope, secret_key, e)


def _credentials_for(conn: dict, client_secret: str | None = None) -> tuple[str, str]:
    if conn.get("auth_mode", AUTH_CUSTOM_SP) == AUTH_APP_SP:
        return _app_sp_credentials()
    if client_secret is None:
        client_secret = _read_secret(conn["secret_scope"], conn["secret_key"])
    return conn["client_id"], client_secret


# ── Token exchange + remote client ───────────────────────────────────────


def fetch_m2m_token(host: str, client_id: str, client_secret: str) -> tuple[str, float]:
    """OAuth client-credentials token from a workspace OIDC endpoint.

    Returns ``(access_token, expires_at_monotonic)``.
    """
    import requests

    resp = requests.post(
        f"{host.rstrip('/')}/oidc/v1/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token endpoint returned {resp.status_code}: {resp.text[:300]}")
    body = resp.json() or {}
    token = body.get("access_token")
    if not token:
        raise RuntimeError("token endpoint response had no access_token")
    expires_in = float(body.get("expires_in") or 3600)
    return token, time.monotonic() + expires_in


class RemoteM2MCredentials(CredentialsStrategy):
    """SDK credentials strategy: client-credentials token, refreshed before expiry."""

    def __init__(self, host: str, client_id: str, client_secret: str):
        self._host = host
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def auth_type(self) -> str:
        return "workbench-remote-m2m"

    def token(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() > self._expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
                self._token, self._expires_at = fetch_m2m_token(
                    self._host, self._client_id, self._client_secret
                )
            return self._token

    def __call__(self, cfg):
        return lambda: {"Authorization": f"Bearer {self.token()}"}


def build_remote_client(conn: dict, client_secret: str | None = None, *, cache: bool = True) -> WorkspaceClient:
    """Build a WorkspaceClient for a remote workspace (cached by connection id)."""
    conn_id = conn["workspace_id"]
    if cache and client_secret is None:
        hit = _remote_clients.get(conn_id)
        if hit and time.monotonic() - hit[0] < _CLIENT_TTL_SECONDS:
            return hit[1]
    client_id, secret = _credentials_for(conn, client_secret)
    creds = RemoteM2MCredentials(conn["host"], client_id, secret)
    creds.token()  # fail fast on bad credentials
    cfg = Config(
        host=conn["host"],
        credentials_strategy=creds,
        client_id=None,
        client_secret=None,  # gitleaks:allow
        http_timeout_seconds=300,
    )
    # The Apps runtime injects the HOME workspace identity as env vars, which the
    # SDK loads into any Config. Clear them so nothing home-scoped leaks into
    # the remote client (it authenticates purely via the strategy above).
    # (ConfigAttribute setters stringify values, so drop keys rather than
    # assigning None.)
    for attr in ("workspace_id", "account_id", "client_id", "client_secret"):
        cfg._inner.pop(attr, None)
    if conn.get("remote_workspace_id"):
        cfg.workspace_id = str(conn["remote_workspace_id"])
    client = WorkspaceClient(config=cfg, product=PRODUCT_NAME, product_version=PRODUCT_VERSION)
    if cache and client_secret is None:
        _remote_clients[conn_id] = (time.monotonic(), client)
    return client


def invalidate_client_cache(conn_id: str) -> None:
    _remote_clients.pop(conn_id, None)


def validate_connection(client: WorkspaceClient) -> dict:
    """Probe a remote client.

    Returns ``{ok, detail, user, remote_workspace_id, system_tables, warehouses}``.
    """
    result: dict = {
        "ok": False, "detail": "", "user": None, "remote_workspace_id": None,
        "system_tables": False, "warehouses": 0,
    }
    try:
        me = client.current_user.me()
        result["user"] = me.user_name or me.display_name
    except Exception as e:  # noqa: BLE001
        result["detail"] = f"Authentication failed: {_short(e)}"
        return result
    result["ok"] = True
    result["detail"] = "Authenticated"
    try:
        result["remote_workspace_id"] = str(client.get_workspace_id())
    except Exception:  # noqa: BLE001
        logger.debug("could not resolve remote workspace id", exc_info=True)
    try:
        result["warehouses"] = sum(1 for _ in client.warehouses.list())
    except Exception:  # noqa: BLE001
        logger.info("warehouse probe failed for remote workspace", exc_info=True)
    try:
        list(client.schemas.list(catalog_name="system"))
        result["system_tables"] = True
    except Exception:  # noqa: BLE001
        logger.info("system-table probe failed for remote workspace", exc_info=True)
    notes = []
    if not result["system_tables"]:
        notes.append("system tables not readable (monitoring will be limited)")
    if not result["warehouses"]:
        notes.append("no SQL warehouse visible (scans, monitoring and Auto-Optimize need one)")
    if notes:
        result["detail"] = "Authenticated — " + "; ".join(notes)
    return result


def _short(e: Exception) -> str:
    text = str(e).splitlines()[0] if str(e) else type(e).__name__
    return text[:300]


def _status_from(validation: dict) -> str:
    if not validation["ok"]:
        return "error"
    return "active" if validation["system_tables"] else "active_no_system_tables"


# ── Connection CRUD (used by routers/workspaces.py) ──────────────────────


def public_view(conn: dict) -> dict:
    """A connection without secret-scope references (safe for API responses)."""
    return {
        "workspace_id": conn["workspace_id"],
        "name": conn["name"],
        "host": conn["host"],
        "auth_mode": conn.get("auth_mode", AUTH_CUSTOM_SP),
        "client_id": conn.get("client_id"),
        "remote_workspace_id": conn.get("remote_workspace_id"),
        "warehouse_id": conn.get("warehouse_id"),
        "status": conn.get("status", "unknown"),
        "status_detail": conn.get("status_detail"),
        "created_by": conn.get("created_by"),
        "created_at": conn.get("created_at"),
        "updated_at": conn.get("updated_at"),
    }


async def list_connections() -> list[dict]:
    return [public_view(r) for r in await state_store.list_workspace_connections()]


async def get_connection(conn_id: str) -> dict | None:
    return await state_store.get_workspace_connection(conn_id)


async def create_connection(
    *,
    name: str,
    host: str,
    auth_mode: str = AUTH_CUSTOM_SP,
    client_id: str | None = None,
    client_secret: str | None = None,
    warehouse_id: str | None = None,
    created_by: str | None = None,
) -> dict:
    """Validate credentials, store any secret, and persist the connection."""
    host = normalize_host(host)
    if auth_mode not in (AUTH_APP_SP, AUTH_CUSTOM_SP):
        raise ValueError("auth_mode must be 'app_sp' or 'custom_sp'")
    if not (name and host):
        raise ValueError("name and host are required")
    if auth_mode == AUTH_CUSTOM_SP and not (client_id and client_secret):
        raise ValueError("client_id and client_secret are required for a custom service principal")

    conn_id = _new_connection_id()
    conn = {
        "workspace_id": conn_id,
        "name": name,
        "host": host,
        "auth_mode": auth_mode,
        "client_id": client_id if auth_mode == AUTH_CUSTOM_SP else app_sp_client_id(),
        "secret_scope": SECRET_SCOPE if auth_mode == AUTH_CUSTOM_SP else None,
        "secret_key": conn_id if auth_mode == AUTH_CUSTOM_SP else None,
        "warehouse_id": (warehouse_id or "").strip() or None,
        "created_by": created_by,
    }
    if any(c["host"] == host for c in await state_store.list_workspace_connections()):
        raise ValueError(f"A connection for {host} already exists")

    client = build_remote_client(conn, client_secret=client_secret, cache=False)
    validation = validate_connection(client)
    if not validation["ok"]:
        raise ValueError(validation["detail"])
    if auth_mode == AUTH_CUSTOM_SP:
        _put_secret(conn_id, client_secret or "")
    conn.update(
        remote_workspace_id=validation["remote_workspace_id"],
        status=_status_from(validation),
        status_detail=validation["detail"],
    )
    await state_store.upsert_workspace_connection(conn)
    return public_view(conn)


async def update_connection(
    conn_id: str,
    *,
    name: str | None = None,
    host: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    warehouse_id: str | None = None,
) -> dict:
    """Update a connection; re-validates when credentials or host change."""
    existing = await state_store.get_workspace_connection(conn_id)
    if not existing:
        raise KeyError(conn_id)
    merged = dict(existing)
    if name is not None:
        merged["name"] = name
    if host is not None:
        merged["host"] = normalize_host(host)
    if warehouse_id is not None:
        merged["warehouse_id"] = warehouse_id.strip() or None
    is_custom = merged.get("auth_mode", AUTH_CUSTOM_SP) == AUTH_CUSTOM_SP
    if client_id is not None and is_custom:
        merged["client_id"] = client_id

    if host is not None or (is_custom and (client_id is not None or client_secret is not None)):
        client = build_remote_client(merged, client_secret=client_secret, cache=False)
        validation = validate_connection(client)
        if not validation["ok"]:
            raise ValueError(validation["detail"])
        merged.update(
            remote_workspace_id=validation["remote_workspace_id"],
            status=_status_from(validation),
            status_detail=validation["detail"],
        )
        if is_custom and client_secret is not None:
            _put_secret(merged["secret_key"], client_secret)

    await state_store.upsert_workspace_connection(merged)
    invalidate_client_cache(conn_id)
    return public_view(merged)


async def delete_connection(conn_id: str) -> None:
    existing = await state_store.get_workspace_connection(conn_id)
    if existing and existing.get("secret_scope") and existing.get("secret_key"):
        _delete_secret(existing["secret_scope"], existing["secret_key"])
    await state_store.delete_workspace_connection(conn_id)
    invalidate_client_cache(conn_id)


async def test_connection(conn_id: str) -> dict:
    """Re-probe a stored connection and persist the fresh status."""
    existing = await state_store.get_workspace_connection(conn_id)
    if not existing:
        raise KeyError(conn_id)
    invalidate_client_cache(conn_id)
    try:
        validation = validate_connection(build_remote_client(existing))
    except Exception as e:  # noqa: BLE001 - token exchange failure
        validation = {"ok": False, "detail": f"Authentication failed: {_short(e)}",
                      "user": None, "remote_workspace_id": None, "system_tables": False, "warehouses": 0}
    status = _status_from(validation)
    await state_store.update_workspace_connection_status(conn_id, status, validation["detail"])
    if validation.get("remote_workspace_id") and not existing.get("remote_workspace_id"):
        await state_store.upsert_workspace_connection(
            {**existing, "remote_workspace_id": validation["remote_workspace_id"],
             "status": status, "status_detail": validation["detail"]}
        )
    return {**validation, "status": status}


async def get_client_for_workspace(conn_id: str) -> WorkspaceClient | None:
    """Cached/built remote client for a connection id, or None if unknown."""
    hit = _remote_clients.get(conn_id)
    if hit and time.monotonic() - hit[0] < _CLIENT_TTL_SECONDS:
        return hit[1]
    conn = await state_store.get_workspace_connection(conn_id)
    if not conn:
        return None
    return build_remote_client(conn)


def resolve_remote_warehouse(conn: dict | None, client: WorkspaceClient) -> str:
    """Warehouse to use in a remote workspace: the configured one, else the
    first running serverless/pro warehouse the remote SP can see."""
    if conn and conn.get("warehouse_id"):
        return conn["warehouse_id"]
    best = ""
    try:
        for wh in client.warehouses.list():
            state = str(getattr(wh.state, "value", wh.state) or "")
            if state == "RUNNING":
                return wh.id or ""
            best = best or (wh.id or "")
    except Exception:  # noqa: BLE001
        logger.warning("Could not list warehouses in the remote workspace", exc_info=True)
    return best
