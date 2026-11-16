"""Low-level Delta key/value table access for the Workbench state store.

The Workbench persists its own state (scan scores, stars, GenieWatch caches,
app settings, Create Agent sessions, cross-workspace connections, ...) in a
single Unity Catalog **Delta** table, replacing the former Lakebase Postgres
schema. Lakebase is not available in every region (e.g. GCP us-east1), while
Delta + a SQL warehouse is available everywhere the app runs.

Table layout::

    <catalog>.<schema>.workbench_state (
        store      STRING,     -- logical table, e.g. "scans", "stars"
        k          STRING,     -- primary key within the store
        v          STRING,     -- JSON payload
        updated_at TIMESTAMP
    ) USING DELTA CLUSTER BY (store, k)

All statements run on the app's SQL warehouse as the **home** service
principal, so they never follow a cross-workspace target and never depend on
the calling user's permissions. ``services.state_store`` owns caching and
flush scheduling; this module only speaks SQL.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

STATE_TABLE = "workbench_state"

# Max size of one MERGE statement's VALUES payload. The Statement Execution API
# caps a statement at 16 MiB; stay far below to leave room for SQL overhead.
_MAX_STATEMENT_BYTES = 4 * 1024 * 1024
_POLL_INTERVAL_SECONDS = 1.0
_STATEMENT_TIMEOUT_SECONDS = 300


class DeltaStoreNotConfigured(RuntimeError):
    """Raised when no storage location or SQL warehouse is configured."""


# ── Location ─────────────────────────────────────────────────────────────


def location() -> tuple[str, str] | None:
    """Return ``(catalog, schema)`` for the state tables, or None if unset."""
    from backend.services import app_config

    return app_config.location()


def warehouse_id() -> str:
    from genie_space_optimizer.common.warehouse import resolve_warehouse_id

    return resolve_warehouse_id() or os.environ.get("SQL_WAREHOUSE_ID", "").strip()


def table_name() -> str:
    loc = location()
    if not loc:
        raise DeltaStoreNotConfigured("Workbench storage location is not configured")
    catalog, schema = loc
    return f"{quote_ident(catalog)}.{quote_ident(schema)}.{STATE_TABLE}"


def available() -> bool:
    """Whether a storage location and a warehouse are both configured."""
    return location() is not None and bool(warehouse_id())


# ── Quoting ──────────────────────────────────────────────────────────────


def quote_ident(name: str) -> str:
    """Backtick-quote a UC identifier part (backticks doubled per Databricks SQL)."""
    return "`" + str(name).replace("`", "``") + "`"


def lit(value: Any) -> str:
    """Render a Python value as a Databricks SQL string literal (or NULL)."""
    if value is None:
        return "NULL"
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


# ── Statement execution ──────────────────────────────────────────────────


def _client():
    # State is always home-scoped — never a cross-workspace target.
    from backend.services.auth import get_home_service_principal_client

    return get_home_service_principal_client()


def run_statement(sql: str, *, client=None, warehouse: str | None = None) -> list[dict]:
    """Execute one statement and return its rows as dicts (all chunks).

    Polls until the statement finishes (bounded by a timeout) instead of
    failing when the warehouse is still starting, and follows result chunks so
    large reads are not truncated to the first chunk.
    """
    from databricks.sdk.service.sql import (
        Disposition,
        ExecuteStatementRequestOnWaitTimeout,
        Format,
        StatementState,
    )

    ws = client or _client()
    wh = warehouse or warehouse_id()
    if not wh:
        raise DeltaStoreNotConfigured("No SQL warehouse is configured")

    resp = ws.statement_execution.execute_statement(
        warehouse_id=wh,
        statement=sql,
        wait_timeout="30s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
    )
    deadline = time.monotonic() + _STATEMENT_TIMEOUT_SECONDS
    pending = {StatementState.PENDING, StatementState.RUNNING}
    while resp.status and resp.status.state in pending:
        if time.monotonic() > deadline:
            try:
                ws.statement_execution.cancel_execution(resp.statement_id)
            except Exception:  # noqa: BLE001 - best effort
                pass
            raise TimeoutError("Delta state statement timed out")
        time.sleep(_POLL_INTERVAL_SECONDS)
        resp = ws.statement_execution.get_statement(resp.statement_id)

    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        err = ""
        if resp.status and resp.status.error:
            err = resp.status.error.message or str(resp.status.error)
        raise RuntimeError(f"Delta state statement failed ({state}): {err}")

    cols = []
    if resp.manifest and resp.manifest.schema and resp.manifest.schema.columns:
        cols = [str(c.name or "") for c in resp.manifest.schema.columns]
    rows: list[dict] = []
    result = resp.result
    while result is not None:
        for row in result.data_array or []:
            rows.append(dict(zip(cols, row)))
        nxt = result.next_chunk_index
        if nxt is None:
            break
        result = ws.statement_execution.get_statement_result_chunk_n(resp.statement_id, nxt)
    return rows


# ── Schema ───────────────────────────────────────────────────────────────


def ensure_table() -> None:
    """Create the state table if missing (idempotent). The schema must exist."""
    run_statement(
        f"CREATE TABLE IF NOT EXISTS {table_name()} ("
        "store STRING NOT NULL, k STRING NOT NULL, v STRING, updated_at TIMESTAMP"
        ") USING DELTA CLUSTER BY (store, k) "
        "COMMENT 'Genie Workbench app state (replaces Lakebase)'"
    )


# ── Reads ────────────────────────────────────────────────────────────────


def load_stores(stores: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Read every row of the given stores as ``{store: [(k, v), ...]}``."""
    if not stores:
        return {}
    in_list = ", ".join(lit(s) for s in stores)
    rows = run_statement(f"SELECT store, k, v FROM {table_name()} WHERE store IN ({in_list})")
    out: dict[str, list[tuple[str, str]]] = {s: [] for s in stores}
    for r in rows:
        out.setdefault(str(r.get("store") or ""), []).append(
            (str(r.get("k") or ""), r.get("v") or "")
        )
    return out


def get_value(store: str, key: str) -> str | None:
    """Point-read one value (used for lazily-loaded stores like sessions)."""
    rows = run_statement(
        f"SELECT v FROM {table_name()} WHERE store = {lit(store)} AND k = {lit(key)} LIMIT 1"
    )
    return rows[0].get("v") if rows else None


# ── Writes ───────────────────────────────────────────────────────────────


def _batches(items: list[tuple[str, str]]):
    batch: list[tuple[str, str]] = []
    size = 0
    for k, v in items:
        row_bytes = len(k) + len(v or "") + 16
        if batch and size + row_bytes > _MAX_STATEMENT_BYTES:
            yield batch
            batch, size = [], 0
        batch.append((k, v))
        size += row_bytes
    if batch:
        yield batch


def upsert(store: str, items: list[tuple[str, str]]) -> None:
    """MERGE ``(k, v)`` rows into ``store`` (keys must be unique)."""
    table = table_name()
    for batch in _batches(items):
        values = ", ".join(f"({lit(k)}, {lit(v)})" for k, v in batch)
        run_statement(
            f"MERGE INTO {table} t "
            f"USING (SELECT * FROM VALUES {values} AS s(k, v)) s "
            f"ON t.store = {lit(store)} AND t.k = s.k "
            "WHEN MATCHED THEN UPDATE SET t.v = s.v, t.updated_at = current_timestamp() "
            "WHEN NOT MATCHED THEN INSERT (store, k, v, updated_at) "
            f"VALUES ({lit(store)}, s.k, s.v, current_timestamp())"
        )


def delete(store: str, keys: list[str]) -> None:
    """Delete the given keys from ``store``."""
    table = table_name()
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        in_list = ", ".join(lit(k) for k in chunk)
        run_statement(f"DELETE FROM {table} WHERE store = {lit(store)} AND k IN ({in_list})")
