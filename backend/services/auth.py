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

# Per-request TARGET workspace (cross-workspace support). Set by the target
# middleware from the ``X-Target-Workspace`` header when an admin selects a
# remote workspace in the same account. While set, workspace-scoped SDK calls
# (Genie, Unity Catalog, system tables, space CRUD) run against the remote
# workspace's service-principal client. Home-only concerns — LLM inference, the
# Delta state store, secret scopes, GSO job orchestration, the caller's own
# identity — must use the ``get_home_*`` accessors so they ignore the target.
_target_client: ContextVar[WorkspaceClient | None] = ContextVar("_target_client", default=None)
_target_workspace_id: ContextVar[str | None] = ContextVar("_target_workspace_id", default=None)
_target_warehouse_id: ContextVar[str | None] = ContextVar("_target_warehouse_id", default=None)


def set_target_workspace(
    workspace_id: str, client: WorkspaceClient, warehouse_id: str | None = None
) -> None:
    """Select a remote target workspace (and its SP client) for this request.

    ``warehouse_id`` is the connection's configured SQL warehouse in the remote
    workspace, if any (otherwise one is auto-detected on first use).
    """
    _target_workspace_id.set(workspace_id)
    _target_client.set(client)
    _target_warehouse_id.set(warehouse_id or None)


def clear_target_workspace() -> None:
    """Return the current request to the home workspace."""
    _target_workspace_id.set(None)
    _target_client.set(None)
    _target_warehouse_id.set(None)


def get_target_warehouse_id() -> str | None:
    """The configured warehouse for the selected remote workspace, if any."""
    return _target_warehouse_id.get()


# Agent-only override (Auto-Optimize). The Auto-Optimize router keeps its data
# plane at home (GSO Delta tables, the optimizer job, MLflow), so it is excluded
# from the full target switch above. For a remote agent it binds this narrower
# override instead: only user-scoped agent reads (require_obo_workspace_client)
# and space-access checks go to the remote workspace; get_workspace_client /
# get_service_principal_client stay home.
_agent_client: ContextVar[WorkspaceClient | None] = ContextVar("_agent_client", default=None)


def set_agent_client(client: WorkspaceClient | None):
    """Bind the remote agent client for this request; returns a reset token."""
    return _agent_client.set(client)


def reset_agent_client(token) -> None:
    _agent_client.reset(token)


def get_agent_client() -> WorkspaceClient | None:
    """The bound remote agent client (Auto-Optimize), or None."""
    return _agent_client.get()


def get_target_workspace_id() -> str | None:
    """The selected remote workspace id for this request, or None for home."""
    return _target_workspace_id.get()


def get_target_client() -> WorkspaceClient | None:
    """The selected remote workspace's SP client, or None for home."""
    return _target_client.get()


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
        # Genie Agent creation can take longer than the SDK's 60s default read
        # timeout (large serialized_space configs, warehouse binding). Widen it
        # so slow-but-successful creates return their response instead of
        # raising a read-timeout that used to trigger duplicate-creation retries.
        http_timeout_seconds=300,
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
        # Widen the read timeout to match the OBO client. Genie Space creation
        # can fall back to the SP on scope errors, and create can exceed the
        # SDK's 60s default read timeout on large configs — without this, an SP
        # create would hit the same timeout that drove the duplicate-space bug.
        _client = WorkspaceClient(
            config=Config(http_timeout_seconds=300),
            product=PRODUCT_NAME,
            product_version=PRODUCT_VERSION,
        )

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


def get_home_workspace_client() -> WorkspaceClient:
    """Home-workspace client (OBO if set, else the SP), ignoring any target.

    Use for operations that always run where the app is deployed: LLM/serving
    inference, the caller's own identity, and home-host display.
    """
    obo = _obo_client.get()
    if obo is not None:
        return obo
    return _get_default_client()


def get_workspace_client() -> WorkspaceClient:
    """Get the WorkspaceClient for the current context.

    A selected remote target wins (its service-principal client). Otherwise
    returns the OBO (per-user) client if set, else the home SP singleton, so
    all SDK calls in the request path use the user's credentials on Apps.
    """
    target = _target_client.get()
    if target is not None:
        return target
    return get_home_workspace_client()


def require_obo_workspace_client() -> WorkspaceClient:
    """Return only the request's authorized client — never the home SP.

    For a remote target there is no per-user identity in that workspace, so
    the remote SP client is returned (targeting is admin-gated). On home this
    never falls back to the app service principal; use it for reads whose
    visibility is explicitly scoped to the current user's permissions.
    """
    target = _target_client.get() or _agent_client.get()
    if target is not None:
        return target
    obo = _obo_client.get()
    if obo is None:
        raise RuntimeError("This operation requires user authorization")
    return obo


def bounded_read_client(client, *, http_timeout_seconds: int = 30):
    """Clone ``client`` with a 1 s SDK retry window and a short HTTP timeout.

    The SDK retries throttling and 503s internally for up to 300 s per call. The
    space access resolver checks through this clone on a cache miss, so a stuck
    Genie read cannot pin a request's worker thread for that 300 s default.
    Falls back to ``client`` if the config cannot be cloned.
    """
    import copy

    try:
        from databricks.sdk import WorkspaceClient

        cfg = copy.deepcopy(client.config)
        cfg.retry_timeout_seconds = 1
        cfg.http_timeout_seconds = http_timeout_seconds
        return WorkspaceClient(config=cfg)
    except Exception:  # noqa: BLE001 - correct auth beats the bounded read
        logger.warning("Could not build a bounded read client; using the default", exc_info=True)
        return client


def get_service_principal_client() -> WorkspaceClient:
    """Get the service principal client (bypasses OBO).

    Follows a selected remote target (returns the remote SP client) so
    workspace-scoped SP operations — system tables, permissions, the Genie
    scope-error fallback — run in the selected workspace. App-level concerns
    that must stay home use :func:`get_home_service_principal_client`.
    """
    target = _target_client.get()
    if target is not None:
        return target
    return _get_default_client()


def get_home_service_principal_client() -> WorkspaceClient:
    """The app's own SP client in the home (deploying) workspace, ignoring target.

    Use for: the Delta state store, secret-scope reads/writes for remote
    connection credentials, GSO job orchestration, and background tasks.
    """
    return _get_default_client()


def get_databricks_host() -> str:
    """Workspace host URL (no trailing slash) for the current context.

    The remote target's host when one is selected (so links point at the
    remote workspace), else the home host.
    """
    target = _target_client.get()
    if target is not None:
        host = target.config.host
        return host.rstrip("/") if host else ""
    return get_home_databricks_host()


def get_home_databricks_host() -> str:
    """Home workspace host URL (no trailing slash), ignoring any target."""
    client = _get_default_client()
    host = client.config.host
    return host.rstrip("/") if host else ""


def get_llm_api_key() -> str:
    """API key for LLM serving endpoints — always the home workspace."""
    client = get_home_workspace_client()
    return client.config.token or os.environ.get("DATABRICKS_TOKEN", "")


# ── Workspace-admin check (cross-workspace authorization) ─────────────────
# Remote targeting and connection management are admin-only. Admin status is
# resolved from the caller's HOME identity and cached per user, so a SCIM call
# isn't issued on every request.
_admin_cache: dict[str, tuple[float, bool]] = {}
_ADMIN_CACHE_TTL = 300  # seconds


def _groups_include_admins(groups_header: str) -> bool:
    return any(g.strip().lower() == "admins" for g in (groups_header or "").split(","))


def _is_admin_via_scim() -> bool:
    """Workspace ``admins`` membership from the caller's own SCIM record (home OBO)."""
    obo = _obo_client.get()
    if obo is None:
        return False
    try:
        me = obo.current_user.me()
        for g in me.groups or []:
            name = getattr(g, "display", None) or ""
            if name.strip().lower() == "admins":
                return True
    except Exception:  # noqa: BLE001 - fail closed
        logger.warning("SCIM admin check failed", exc_info=True)
    return False


def is_workspace_admin(*, forwarded_groups: str = "", forwarded_email: str = "") -> bool:
    """Whether the current caller is a home-workspace admin.

    Order: local-dev hatch (only when NOT on Databricks Apps), an exact
    ``admins`` entry in the forwarded groups header, then a cached SCIM lookup
    with the caller's own token. Databricks Apps does not always forward
    ``X-Forwarded-Groups``, so the SCIM path is what usually decides.
    """
    if not is_running_on_databricks_apps():
        if os.environ.get("DEV_ADMIN", "").lower() == "true" or os.environ.get("DEV_USER_EMAIL"):
            return True
    if _groups_include_admins(forwarded_groups):
        return True
    if not forwarded_email:
        return False
    now = time.monotonic()
    hit = _admin_cache.get(forwarded_email)
    if hit is not None and now - hit[0] < _ADMIN_CACHE_TTL:
        return hit[1]
    result = _is_admin_via_scim()
    _admin_cache[forwarded_email] = (now, result)
    return result


def is_admin_request(request) -> bool:
    """:func:`is_workspace_admin` for a FastAPI/Starlette request."""
    headers = request.headers
    return is_workspace_admin(
        forwarded_groups=headers.get("X-Forwarded-Groups", ""),
        forwarded_email=(
            headers.get("X-Forwarded-Email")
            or headers.get("X-Forwarded-Preferred-Username")
            or headers.get("X-Forwarded-User")
            or ""
        ),
    )


def run_in_context(fn, *args, **kwargs):
    """Capture current contextvars and return a zero-arg callable that
    runs fn(*args, **kwargs) in that snapshot.

    Python <3.12 does not propagate contextvars into thread-pool threads.
    Use with loop.run_in_executor or ThreadPoolExecutor.submit:

        await loop.run_in_executor(None, run_in_context(handle_tool_call, n, a, cfg))
    """
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn, *args, **kwargs)
