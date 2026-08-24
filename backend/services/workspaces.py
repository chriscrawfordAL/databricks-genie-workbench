"""Cross-workspace remote connections.

The app is deployed in one *home* workspace (east1) but can deploy, score, and
monitor Genie Agents in *remote* workspaces. Each remote workspace is reached
with its own service-principal OAuth (M2M) credentials.

Design:
- Connection metadata (name, host, client_id) + a secret-scope *reference*
  live in Lakebase (``genie.workspace_connections``). See ``services/lakebase``.
- The SP client *secret* is stored in a Databricks secret scope in the HOME
  workspace (never in Lakebase). Only ``(secret_scope, secret_key)`` are
  persisted alongside the connection.
- A ``WorkspaceClient`` for a remote workspace is built lazily from those creds
  (``auth_type="oauth-m2m"``) and cached by ``workspace_id``. The SDK refreshes
  the M2M token internally, so the cached client stays valid.

All secret-scope operations use the HOME service-principal client
(``get_home_service_principal_client``) so they never follow the selected
target workspace.
"""

import base64
import logging
import uuid

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

from backend._telemetry import PRODUCT_NAME, PRODUCT_VERSION
from backend.services import lakebase
from backend.services.auth import get_home_service_principal_client

logger = logging.getLogger(__name__)

# Databricks secret scope (in the home workspace) that holds remote SP secrets.
SECRET_SCOPE = "genie-workbench-workspaces"

# Cache of built remote clients, keyed by workspace_id.
_remote_clients: dict[str, WorkspaceClient] = {}


def _normalize_host(host: str) -> str:
    """Normalize a workspace host to ``https://<host>`` with no trailing slash."""
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


def _new_workspace_id() -> str:
    return f"ws_{uuid.uuid4().hex[:16]}"


# ── Secret storage (home workspace) ──────────────────────────────────────

def _ensure_scope() -> None:
    """Create the secret scope in the home workspace if it doesn't exist."""
    client = get_home_service_principal_client()
    try:
        scopes = client.secrets.list_scopes()
        names = {s.name for s in (scopes or [])}
        if SECRET_SCOPE not in names:
            client.secrets.create_scope(scope=SECRET_SCOPE)
            logger.info("Created secret scope '%s'", SECRET_SCOPE)
    except Exception:
        # create_scope is idempotent-ish; a RESOURCE_ALREADY_EXISTS is fine.
        try:
            client.secrets.create_scope(scope=SECRET_SCOPE)
        except Exception as e:
            if "RESOURCE_ALREADY_EXISTS" not in str(e):
                raise


def _put_secret(secret_key: str, client_secret: str) -> None:
    _ensure_scope()
    client = get_home_service_principal_client()
    client.secrets.put_secret(
        scope=SECRET_SCOPE, key=secret_key, string_value=client_secret
    )


def _read_secret(secret_scope: str, secret_key: str) -> str:
    """Read and decode a stored SP client secret from the home workspace."""
    client = get_home_service_principal_client()
    resp = client.secrets.get_secret(scope=secret_scope, key=secret_key)
    value = resp.value or ""
    # The Secrets API returns the value base64-encoded.
    return base64.b64decode(value).decode("utf-8")


def _delete_secret(secret_scope: str, secret_key: str) -> None:
    client = get_home_service_principal_client()
    try:
        client.secrets.delete_secret(scope=secret_scope, key=secret_key)
    except Exception as e:
        logger.warning("Failed to delete secret %s/%s: %s", secret_scope, secret_key, e)


# ── Remote client factory ────────────────────────────────────────────────

def build_remote_client(conn: dict, client_secret: str | None = None) -> WorkspaceClient:
    """Build (and cache) a WorkspaceClient for a remote workspace via SP M2M.

    ``client_secret`` may be passed directly (validation before persistence);
    otherwise it is read from the stored secret scope.
    """
    workspace_id = conn["workspace_id"]
    if client_secret is None:
        cached = _remote_clients.get(workspace_id)
        if cached is not None:
            return cached
        client_secret = _read_secret(conn["secret_scope"], conn["secret_key"])

    cfg = Config(
        host=conn["host"],
        client_id=conn["client_id"],
        client_secret=client_secret,
        auth_type="oauth-m2m",
    )
    client = WorkspaceClient(config=cfg, product=PRODUCT_NAME, product_version=PRODUCT_VERSION)
    # Only cache clients built from the persisted secret (not ad-hoc validation).
    if conn.get("secret_key"):
        _remote_clients[workspace_id] = client
    return client


def invalidate_client_cache(workspace_id: str) -> None:
    _remote_clients.pop(workspace_id, None)


def validate_connection(client: WorkspaceClient) -> dict:
    """Probe a remote client. Returns {'ok', 'detail', 'user', 'system_tables'}."""
    result: dict = {"ok": False, "detail": "", "user": None, "system_tables": False}
    try:
        me = client.current_user.me()
        result["user"] = me.user_name or me.display_name
        result["ok"] = True
        result["detail"] = "Authenticated"
    except Exception as e:
        result["detail"] = f"Authentication failed: {e}"
        return result

    # Best-effort probe: can the SP read system tables (needed for monitoring)?
    try:
        list(client.catalogs.list())
        # A cheap system-schema check.
        client.schemas.list(catalog_name="system")
        result["system_tables"] = True
    except Exception as e:
        result["system_tables"] = False
        result["detail"] = (
            "Authenticated, but system tables are not readable "
            f"(monitoring will be limited): {e}"
        )
    return result


# ── High-level orchestration used by the router ──────────────────────────

async def list_connections() -> list[dict]:
    """Return connections without any secret material."""
    rows = await lakebase.list_workspace_connections()
    return [_public_view(r) for r in rows]


def _public_view(conn: dict) -> dict:
    """Strip secret-scope references from a connection dict for API responses."""
    return {
        "workspace_id": conn["workspace_id"],
        "name": conn["name"],
        "host": conn["host"],
        "client_id": conn["client_id"],
        "status": conn.get("status", "unknown"),
        "status_detail": conn.get("status_detail"),
        "created_by": conn.get("created_by"),
        "created_at": conn.get("created_at"),
        "updated_at": conn.get("updated_at"),
    }


async def create_connection(
    name: str, host: str, client_id: str, client_secret: str, created_by: str | None
) -> dict:
    """Validate creds, store the secret, and persist the connection."""
    host = _normalize_host(host)
    if not (name and host and client_id and client_secret):
        raise ValueError("name, host, client_id and client_secret are required")

    workspace_id = _new_workspace_id()
    secret_key = workspace_id

    # Validate before persisting anything.
    probe_conn = {
        "workspace_id": workspace_id,
        "host": host,
        "client_id": client_id,
        "secret_scope": SECRET_SCOPE,
        "secret_key": "",  # don't cache during validation
    }
    client = build_remote_client(probe_conn, client_secret=client_secret)
    validation = validate_connection(client)
    if not validation["ok"]:
        raise ValueError(validation["detail"])

    # Store the secret in the home workspace, then persist the row.
    _put_secret(secret_key, client_secret)
    status = "active" if validation["system_tables"] else "active_no_system_tables"
    conn_dict = {
        "workspace_id": workspace_id,
        "name": name,
        "host": host,
        "client_id": client_id,
        "secret_scope": SECRET_SCOPE,
        "secret_key": secret_key,
        "status": status,
        "status_detail": validation["detail"],
        "created_by": created_by,
    }
    await lakebase.upsert_workspace_connection(conn_dict)
    return _public_view(conn_dict)


async def update_connection(
    workspace_id: str,
    name: str | None = None,
    host: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict:
    """Update mutable fields of a connection; rotate the secret if provided."""
    existing = await lakebase.get_workspace_connection(workspace_id)
    if not existing:
        raise KeyError(workspace_id)

    merged = dict(existing)
    if name is not None:
        merged["name"] = name
    if host is not None:
        merged["host"] = _normalize_host(host)
    if client_id is not None:
        merged["client_id"] = client_id

    # Re-validate whenever creds change.
    if client_secret is not None or client_id is not None or host is not None:
        secret_for_validation = client_secret
        if secret_for_validation is None:
            secret_for_validation = _read_secret(merged["secret_scope"], merged["secret_key"])
        probe_conn = {**merged, "secret_key": ""}
        client = build_remote_client(probe_conn, client_secret=secret_for_validation)
        validation = validate_connection(client)
        if not validation["ok"]:
            raise ValueError(validation["detail"])
        merged["status"] = "active" if validation["system_tables"] else "active_no_system_tables"
        merged["status_detail"] = validation["detail"]
        if client_secret is not None:
            _put_secret(merged["secret_key"], client_secret)

    await lakebase.upsert_workspace_connection(merged)
    invalidate_client_cache(workspace_id)
    return _public_view(merged)


async def delete_connection(workspace_id: str) -> None:
    existing = await lakebase.get_workspace_connection(workspace_id)
    if existing:
        _delete_secret(existing["secret_scope"], existing["secret_key"])
    await lakebase.delete_workspace_connection(workspace_id)
    invalidate_client_cache(workspace_id)


async def test_connection(workspace_id: str) -> dict:
    """Re-probe a stored connection and persist the fresh status."""
    existing = await lakebase.get_workspace_connection(workspace_id)
    if not existing:
        raise KeyError(workspace_id)
    invalidate_client_cache(workspace_id)
    client = build_remote_client(existing)
    validation = validate_connection(client)
    status = (
        "active"
        if validation["ok"] and validation["system_tables"]
        else "active_no_system_tables"
        if validation["ok"]
        else "error"
    )
    await lakebase.update_workspace_connection_status(workspace_id, status, validation["detail"])
    return {**validation, "status": status}


async def get_client_for_workspace(workspace_id: str) -> WorkspaceClient | None:
    """Return a cached/built remote client for a workspace_id, or None."""
    conn = await lakebase.get_workspace_connection(workspace_id)
    if not conn:
        return None
    return build_remote_client(conn)
