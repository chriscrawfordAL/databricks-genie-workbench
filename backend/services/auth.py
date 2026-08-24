"""
Authentication utilities for Databricks Apps deployment.

On Databricks Apps, uses OBO (On Behalf Of) — each request creates a
WorkspaceClient with the user's forwarded token so all SDK calls (SQL,
UC, serving endpoints) execute under the user's identity and permissions.

Locally, falls back to PAT token or CLI profile (singleton client).
"""

import contextvars
import logging
import os
import time
from contextvars import ContextVar

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

from backend._telemetry import PRODUCT_NAME, PRODUCT_VERSION

logger = logging.getLogger(__name__)

# Singleton client for local dev (or fallback when no user token is available)
_client: WorkspaceClient | None = None
_auth_logged = False

# Per-request OBO client stored in a context variable
_obo_client: ContextVar[WorkspaceClient | None] = ContextVar("_obo_client", default=None)

# Per-request TARGET workspace, set from the X-Target-Workspace header when the
# user selects a remote workspace. When set, workspace-scoped SDK calls (Genie,
# Unity Catalog, system tables, space CRUD) run against the remote workspace's
# service-principal client. Home-only concerns (LLM/serving inference, Lakebase,
# secret scopes, the caller's identity) must use the get_home_* accessors so
# they ignore the target.
_target_client: ContextVar[WorkspaceClient | None] = ContextVar("_target_client", default=None)
_target_workspace_id: ContextVar[str | None] = ContextVar("_target_workspace_id", default=None)


def set_target_workspace(workspace_id: str, client: WorkspaceClient) -> None:
    """Set the remote target workspace + its client for the current request."""
    _target_workspace_id.set(workspace_id)
    _target_client.set(client)


def clear_target_workspace() -> None:
    """Clear the target workspace for the current request."""
    _target_workspace_id.set(None)
    _target_client.set(None)


def get_target_workspace_id() -> str | None:
    """Return the selected remote workspace id for this request, or None (home)."""
    return _target_workspace_id.get()


def is_running_on_databricks_apps() -> bool:
    """Check if running on Databricks Apps (vs local development)."""
    return os.environ.get("DATABRICKS_APP_PORT") is not None


def set_obo_user_token(token: str) -> None:
    """Set the user's OBO token for the current request context.

    Call this from middleware/dependencies with the user's Authorization
    header value. Creates a per-request WorkspaceClient that authenticates
    as the user.

    We must explicitly set ``auth_type="pat"`` because the Databricks Apps
    environment has DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET set,
    and the SDK would otherwise use oauth-m2m instead of the user's token.
    """
    host = os.environ.get("DATABRICKS_HOST", "")
    if not host:
        default = _get_default_client()
        host = default.config.host or ""

    cfg = Config(
        host=host,
        token=token,
        auth_type="pat",
        # Prevent the SDK from reading env vars that would override the token
        client_id=None,
        client_secret=None,  # gitleaks:allow
    )
    client = WorkspaceClient(config=cfg, product=PRODUCT_NAME, product_version=PRODUCT_VERSION)
    _obo_client.set(client)
    logger.debug("OBO client set for current request (host=%s, auth=%s)", host, cfg.auth_type)


def clear_obo_user_token() -> None:
    """Clear the per-request OBO client after the request completes."""
    _obo_client.set(None)


def _get_default_client() -> WorkspaceClient:
    """Get the default singleton client (SP on Apps, CLI/PAT locally)."""
    global _client, _auth_logged

    if _client is None:
        _client = WorkspaceClient(product=PRODUCT_NAME, product_version=PRODUCT_VERSION)

        if not _auth_logged:
            logger.info("=== Databricks SDK Authentication ===")
            logger.info(f"  Host: {_client.config.host}")
            logger.info(f"  Auth type: {_client.config.auth_type}")
            logger.info(f"  Running on Databricks Apps: {is_running_on_databricks_apps()}")

            env_vars = [
                "DATABRICKS_HOST",
                "DATABRICKS_APP_PORT",
                "DATABRICKS_CLIENT_ID",
                "DATABRICKS_TOKEN",
            ]
            for var in env_vars:
                val = os.environ.get(var)
                if val:
                    if "TOKEN" in var or "SECRET" in var:
                        logger.info(f"  {var}: [SET]")
                    elif "CLIENT_ID" in var:
                        logger.info(f"  {var}: {val[:8]}...")
                    else:
                        logger.info(f"  {var}: {val}")

            _auth_logged = True

    return _client


def _home_workspace_client() -> WorkspaceClient:
    """The home-workspace client: OBO (per-user) if set, else the SP singleton.

    This is the pre-cross-workspace behaviour of :func:`get_workspace_client`
    and never follows a selected remote target.
    """
    obo = _obo_client.get()
    if obo is not None:
        return obo
    return _get_default_client()


def get_home_workspace_client() -> WorkspaceClient:
    """Home-workspace client, ignoring any selected remote target.

    Use for operations that must run in the deploying workspace regardless of
    the selected target: LLM/serving inference, the caller's own identity, and
    home-host display.
    """
    return _home_workspace_client()


def get_workspace_client() -> WorkspaceClient:
    """Get the WorkspaceClient for the current context.

    If a remote target workspace is selected for this request, returns that
    workspace's service-principal client. Otherwise returns the OBO (per-user)
    client if set, else the home SP singleton.
    """
    target = _target_client.get()
    if target is not None:
        return target
    return _home_workspace_client()


def require_obo_workspace_client() -> WorkspaceClient:
    """Return the request's authorized client, never the home SP fallback.

    For a remote target there is no per-user identity, so the remote SP client
    is returned. On home, returns the OBO client or raises if absent.
    """
    target = _target_client.get()
    if target is not None:
        return target
    obo = _obo_client.get()
    if obo is None:
        raise RuntimeError("This operation requires user authorization")
    return obo


def get_service_principal_client() -> WorkspaceClient:
    """Get the service principal client (bypasses OBO).

    If a remote target workspace is selected, returns the remote SP client so
    workspace-scoped SP operations (system tables, permissions) run there.
    Otherwise returns the home SP singleton. Home-only concerns (Lakebase,
    secret scopes) must use :func:`get_home_service_principal_client`.
    """
    target = _target_client.get()
    if target is not None:
        return target
    return _get_default_client()


def get_home_service_principal_client() -> WorkspaceClient:
    """Get the *home* (deploying) workspace's SP client, ignoring any target.

    Unlike :func:`get_service_principal_client`, this always returns the app's
    own service-principal client for the workspace the app is deployed in
    (east1). Use it for operations that must run against home regardless of the
    selected target workspace — e.g. Databricks secret-scope reads/writes for
    remote-connection credentials, and Lakebase persistence.
    """
    return _get_default_client()


def get_databricks_host() -> str:
    """Get the workspace host URL (without trailing slash) for the current context.

    Returns the remote target's host when one is selected (so space URLs and
    links point at the remote workspace), else the home host.
    """
    target = _target_client.get()
    if target is not None:
        host = target.config.host
        return host.rstrip("/") if host else ""
    return get_home_databricks_host()


def get_home_databricks_host() -> str:
    """Get the home workspace host URL (without trailing slash), ignoring target."""
    client = _get_default_client()
    host = client.config.host
    return host.rstrip("/") if host else ""


def get_llm_api_key() -> str:
    """Get the API key for LLM serving endpoints (always the home workspace)."""
    client = get_home_workspace_client()
    return client.config.token or os.environ.get("DATABRICKS_TOKEN", "")


# ── Workspace-admin check (for cross-workspace authorization) ──────────────
# Cross-workspace targeting and connection management are admin-only. Admin
# status is resolved from the caller's *home* identity and cached per user
# (TTL) so we don't issue a SCIM call on every request.
_admin_cache: dict[str, tuple[float, bool]] = {}
_ADMIN_CACHE_TTL = 300  # seconds


def _is_admin_via_sdk() -> bool:
    """Check workspace-admin membership via SCIM using the home OBO client.

    Databricks Apps does not reliably forward an ``X-Forwarded-Groups`` header,
    so header-only detection rejects real admins. We read the caller's own SCIM
    record (home identity, never a remote target) and look for the workspace
    ``admins`` group.
    """
    try:
        me = get_home_workspace_client().current_user.me()
        for g in (me.groups or []):
            name = (getattr(g, "display", None) or getattr(g, "value", "") or "")
            if name.lower() == "admins":
                return True
    except Exception:
        logger.warning("SDK admin check failed", exc_info=True)
    return False


def is_workspace_admin(*, forwarded_groups: str = "", forwarded_email: str = "") -> bool:
    """Whether the current caller is a workspace admin.

    Order: local-dev escape hatch (only when NOT on Databricks Apps), then the
    forwarded ``admins`` group (fast path), then a cached SCIM lookup. The DEV
    hatch is gated on ``not is_running_on_databricks_apps()`` so a stray
    DEV_USER_EMAIL/DEV_ADMIN in a real deployment can never grant admin.
    """
    if not is_running_on_databricks_apps():
        if os.environ.get("DEV_ADMIN", "").lower() == "true" or os.environ.get("DEV_USER_EMAIL"):
            return True

    if "admins" in (forwarded_groups or "").lower():
        return True

    key = forwarded_email or "<obo>"
    now = time.monotonic()
    hit = _admin_cache.get(key)
    if hit is not None and now - hit[0] < _ADMIN_CACHE_TTL:
        return hit[1]
    result = _is_admin_via_sdk()
    _admin_cache[key] = (now, result)
    return result


def run_in_context(fn, *args, **kwargs):
    """Capture current contextvars and return a zero-arg callable that
    runs fn(*args, **kwargs) in that snapshot.

    Python <3.12 does not propagate contextvars into thread-pool threads.
    Use with loop.run_in_executor or ThreadPoolExecutor.submit:

        await loop.run_in_executor(None, run_in_context(handle_tool_call, n, a, cfg))
    """
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn, *args, **kwargs)
