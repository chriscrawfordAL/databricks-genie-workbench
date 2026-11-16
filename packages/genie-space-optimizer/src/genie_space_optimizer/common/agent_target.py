"""Remote (cross-workspace) agent targeting for the optimizer job.

The optimizer job always runs in the HOME workspace (where the Workbench app
and its GSO Delta state, MLflow experiments and LLM endpoints live). A Genie
Agent may live in a DIFFERENT workspace with a DIFFERENT Unity Catalog
metastore. In that case every operation that touches the agent or the agent's
data must go to the remote workspace:

* Genie API (config fetch/patch, Benchmark/eval API, permissions)
* SQL against the agent's tables (ground truth, EXPLAIN, profiling, DESCRIBE)
  — on a SQL warehouse in the remote workspace, never on the home Spark session
* Unity Catalog metadata reads and grant probes for the agent's tables

while LLM calls, GSO state (``genie_opt_*``), and MLflow stay home.

The app passes a single JSON ``target`` job parameter (see
:class:`AgentTarget`). It carries only a *reference* to the remote service
principal's client secret (a secret scope/key in the home workspace) — never
the secret value. Each job entrypoint calls
:func:`configure_agent_target_from_param` once; the override is a process-wide
module global (one optimization per job process; visible to worker threads).
When no target is configured every helper here is an identity function, so
home-workspace runs are unchanged.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN_SECONDS = 300


class RemoteSparkUnavailable(RuntimeError):
    """A Spark-only path for agent data/metadata was hit during a remote run.

    The home Spark session reads the HOME metastore; the agent's tables live in
    a different metastore, so running agent SQL there would silently produce
    wrong answers. Callers must use the remote warehouse instead.
    """


@dataclass(frozen=True)
class AgentTarget:
    """Where the agent lives. Carries a secret *reference*, never a secret."""

    workspace_id: str
    host: str
    client_id: str
    secret_scope: str
    secret_key: str
    warehouse_id: str | None = None
    remote_workspace_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> "AgentTarget | None":
        text = (raw or "").strip()
        if not text or text == "{}":
            return None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"target is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not data:
            return None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentTarget":
        required = ("workspace_id", "host", "client_id", "secret_scope", "secret_key")
        missing = [k for k in required if not str(data.get(k) or "").strip()]
        if missing:
            raise ValueError(f"target is missing required fields: {', '.join(missing)}")
        host = str(data["host"]).strip().rstrip("/")
        if not host.startswith("https://"):
            raise ValueError("target host must start with https://")
        return cls(
            workspace_id=str(data["workspace_id"]).strip(),
            host=host,
            client_id=str(data["client_id"]).strip(),
            secret_scope=str(data["secret_scope"]).strip(),
            secret_key=str(data["secret_key"]).strip(),
            warehouse_id=(str(data.get("warehouse_id") or "").strip() or None),
            remote_workspace_id=(str(data.get("remote_workspace_id") or "").strip() or None),
        )


# ── Remote client construction ───────────────────────────────────────────


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
        raise RuntimeError(f"remote token endpoint returned {resp.status_code}: {resp.text[:300]}")
    body = resp.json() or {}
    token = body.get("access_token")
    if not token:
        raise RuntimeError("remote token endpoint response had no access_token")
    return token, time.monotonic() + float(body.get("expires_in") or 3600)


def _credentials_strategy(host: str, client_id: str, client_secret: str):
    from databricks.sdk.credentials_provider import CredentialsStrategy

    class _RemoteM2M(CredentialsStrategy):
        def __init__(self) -> None:
            self._token: str | None = None
            self._expires_at = 0.0
            self._lock = threading.Lock()

        def auth_type(self) -> str:
            return "gso-remote-m2m"

        def token(self) -> str:
            with self._lock:
                if (
                    self._token is None
                    or time.monotonic() > self._expires_at - _TOKEN_REFRESH_MARGIN_SECONDS
                ):
                    self._token, self._expires_at = fetch_m2m_token(host, client_id, client_secret)
                return self._token

        def __call__(self, cfg):  # noqa: ARG002 - SDK protocol
            return lambda: {"Authorization": f"Bearer {self.token()}"}

    return _RemoteM2M()


def _read_secret(home_w: "WorkspaceClient", scope: str, key: str) -> str:
    resp = home_w.secrets.get_secret(scope=scope, key=key)
    return base64.b64decode(resp.value or "").decode("utf-8")


def build_agent_client(home_w: "WorkspaceClient", target: AgentTarget) -> "WorkspaceClient":
    """Build a WorkspaceClient for the agent's (remote) workspace."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.config import Config

    from genie_space_optimizer._telemetry import PRODUCT_NAME, PRODUCT_VERSION

    secret = _read_secret(home_w, target.secret_scope, target.secret_key)
    creds = _credentials_strategy(target.host, target.client_id, secret)
    creds.token()  # fail fast on bad credentials
    cfg = Config(
        host=target.host,
        credentials_strategy=creds,
        client_id=None,
        client_secret=None,  # gitleaks:allow
        http_timeout_seconds=300,
    )
    # The job runtime injects the HOME identity as env vars, which the SDK loads
    # into any Config. Clear them so nothing home-scoped leaks into the client.
    # (Assigning None through the attribute would store the string "None" — the
    # SDK's ConfigAttribute setter applies str() — so unset via the inner dict.)
    for name in ("workspace_id", "account_id", "client_id", "client_secret"):
        cfg._inner.pop(name, None)
    if target.remote_workspace_id:
        cfg.workspace_id = target.remote_workspace_id
    return WorkspaceClient(config=cfg, product=PRODUCT_NAME, product_version=PRODUCT_VERSION)


def resolve_agent_warehouse(client: "WorkspaceClient", target: AgentTarget) -> str:
    """The SQL warehouse to use in the agent's workspace."""
    if target.warehouse_id:
        return target.warehouse_id
    running: list[str] = []
    other: list[str] = []
    try:
        for wh in client.warehouses.list():
            wh_type = getattr(getattr(wh, "warehouse_type", None), "value", getattr(wh, "warehouse_type", ""))
            if not (getattr(wh, "enable_serverless_compute", False) or wh_type == "PRO"):
                continue
            state = getattr(getattr(wh, "state", None), "value", getattr(wh, "state", ""))
            (running if str(state).upper() == "RUNNING" else other).append(wh.id or "")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not list SQL warehouses in the agent's workspace: {exc}") from exc
    for wid in running + other:
        if wid:
            return wid
    raise RuntimeError(
        "No Pro or Serverless SQL warehouse is available to the optimizer's service principal "
        f"in the agent's workspace ({target.host}). Grant it CAN_USE on one, or set a warehouse "
        "on the workspace connection."
    )


# ── Process-wide override registry ───────────────────────────────────────

_lock = threading.Lock()
_client: "WorkspaceClient | None" = None
_warehouse: str | None = None
_target: AgentTarget | None = None


def set_agent_target(client: "WorkspaceClient", warehouse_id: str | None, target: AgentTarget | None) -> None:
    global _client, _warehouse, _target
    with _lock:
        _client, _warehouse, _target = client, (warehouse_id or None), target


def clear_agent_target() -> None:
    global _client, _warehouse, _target
    with _lock:
        _client, _warehouse, _target = None, None, None


def remote_active() -> bool:
    return _client is not None


def current_agent_target() -> AgentTarget | None:
    return _target


def agent_client(w: Any) -> Any:
    """The remote agent client when a target is active, else ``w`` unchanged."""
    return _client if _client is not None else w


def agent_warehouse(default: str | None) -> str | None:
    """The remote warehouse when a target is active, else ``default``."""
    return _warehouse if _client is not None else default


def agent_sql(w: Any, warehouse_id: str | None) -> tuple[Any, str | None]:
    """``(client, warehouse_id)`` to run agent-data SQL with."""
    if _client is not None:
        return _client, _warehouse
    return w, warehouse_id


def require_no_remote_spark(what: str) -> None:
    """Raise :class:`RemoteSparkUnavailable` if a remote target is active."""
    if _client is not None:
        raise RemoteSparkUnavailable(
            f"{what} would run on the home Spark session, but the Genie Agent lives in "
            "another workspace/metastore; use the remote SQL warehouse instead."
        )


def configure_agent_target_from_param(home_w: "WorkspaceClient", raw_param: str | None) -> AgentTarget | None:
    """Parse the ``target`` job param, build the remote client, and install it.

    Returns the target (None for a home run, which clears any override).
    """
    target = AgentTarget.from_json(raw_param)
    if target is None:
        clear_agent_target()
        return None
    client = build_agent_client(home_w, target)
    warehouse = resolve_agent_warehouse(client, target)
    try:
        from genie_space_optimizer.common.genie_client import configure_connection_pool

        configure_connection_pool(client)
    except Exception:  # noqa: BLE001 - pool tuning is best effort
        logger.debug("connection pool tuning failed for remote client", exc_info=True)
    set_agent_target(client, warehouse, target)
    logger.info(
        "Remote agent target active: connection=%s host=%s warehouse=%s",
        target.workspace_id, target.host, warehouse,
    )
    return target
