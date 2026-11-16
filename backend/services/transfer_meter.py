"""App-instrumented cross-workspace data-transfer metering.

Measures the volume of data the app pulls IN from remote (non-home) workspaces
so an admin can estimate egress cost. Egress is a cloud cross-region /
cross-cloud network charge with no single Databricks metric, so this counts
response-payload bytes at the app's own outbound call sites (Genie Agent config
fetch, ad-hoc SQL results, and system-table monitoring reads) and attributes
them to the currently selected target workspace
(:func:`backend.services.auth.get_target_workspace_id`).

Only *remote* pulls are counted: when no target workspace is selected the
traffic is home-local and no egress applies, so :func:`record_current` is a
no-op. It follows that this is an app-observed proxy for egress, not the
provider's billed figure — the UI states as much.

Bytes accumulate in a small in-memory buffer keyed by
``(workspace_id, UTC day, category)`` and are flushed to the Workbench Delta state store
(``services.state_store``) periodically and on shutdown, so totals survive
restarts. Metering must never disrupt the request it observes, so recording
swallows all errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from backend.services.auth import get_target_workspace_id

logger = logging.getLogger(__name__)

# Operation categories (kept stable — surfaced in the UI breakdown).
CATEGORY_GENIE = "genie"                   # Genie Agent config fetch / list (deploy, score)
CATEGORY_SQL = "sql"                       # ad-hoc SQL / statement-execution results (score, scan)
CATEGORY_SYSTEM_TABLES = "system_tables"   # monitoring reads from system.* tables
CATEGORY_OTHER = "other"
CATEGORIES = (CATEGORY_GENIE, CATEGORY_SQL, CATEGORY_SYSTEM_TABLES, CATEGORY_OTHER)

_lock = threading.Lock()
# (workspace_id, day_iso, category) -> [bytes, requests]
_buffer: dict[tuple[str, str, str], list[int]] = {}


def estimate_bytes(payload: Any) -> int:
    """Best-effort serialized size of a response payload, in bytes.

    Uses compact JSON as a proxy for the wire size. Never raises — an
    un-serializable payload falls back to its ``str()`` length, and a total
    failure yields 0 so the caller is never disrupted.
    """
    if payload is None:
        return 0
    try:
        return len(json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8"))
    except Exception:
        try:
            return len(str(payload).encode("utf-8"))
        except Exception:
            return 0


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def record(workspace_id: str, category: str, num_bytes: int, requests: int = 1) -> None:
    """Buffer measured bytes for an explicit remote workspace + category."""
    if not workspace_id or num_bytes <= 0:
        return
    if category not in CATEGORIES:
        category = CATEGORY_OTHER
    key = (workspace_id, _today_utc(), category)
    with _lock:
        slot = _buffer.get(key)
        if slot is None:
            _buffer[key] = [int(num_bytes), int(requests)]
        else:
            slot[0] += int(num_bytes)
            slot[1] += int(requests)


def record_current(category: str, payload: Any) -> None:
    """Record a pull against the *currently selected* target workspace.

    No-op when there is no target (home-local traffic → no egress). Sizing the
    payload and buffering are cheap and must never disrupt the caller, so any
    failure is swallowed.
    """
    try:
        wsid = get_target_workspace_id()
        if not wsid:
            return
        record(wsid, category, estimate_bytes(payload))
    except Exception:  # pragma: no cover - metering must never break a request
        logger.debug("transfer metering skipped", exc_info=True)


def _snapshot_and_clear() -> list[dict]:
    with _lock:
        if not _buffer:
            return []
        rows = [
            {"workspace_id": ws, "day": day, "category": cat, "bytes": b, "requests": r}
            for (ws, day, cat), (b, r) in _buffer.items()
        ]
        _buffer.clear()
    return rows


def _rebuffer(rows: list[dict]) -> None:
    """Return unflushed rows to the buffer so the next flush retries them."""
    with _lock:
        for row in rows:
            key = (row["workspace_id"], row["day"], row["category"])
            slot = _buffer.get(key)
            if slot is None:
                _buffer[key] = [row["bytes"], row["requests"]]
            else:
                slot[0] += row["bytes"]
                slot[1] += row["requests"]


async def flush() -> None:
    """Hand buffered counters to the state store (best-effort).

    On failure the rows are re-buffered rather than dropped.
    """
    rows = _snapshot_and_clear()
    if not rows:
        return
    try:
        from backend.services import state_store
        await state_store.add_workspace_transfer(rows)
    except Exception:
        logger.warning("transfer flush failed; re-buffering", exc_info=True)
        _rebuffer(rows)


async def flush_periodically(interval_seconds: int = 30) -> None:
    """Background loop that flushes the buffer on an interval until cancelled."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await flush()
        except asyncio.CancelledError:
            break
        except Exception:  # pragma: no cover - keep the loop alive
            logger.debug("periodic transfer flush error", exc_info=True)
