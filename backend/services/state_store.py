"""Workbench state store — in-memory reads, Delta-table durability.

Replaces the former Lakebase (Postgres) persistence layer. Lakebase is not
available in every region (e.g. GCP us-east1); Unity Catalog Delta tables are.

Design
------
* A process-local cache (``_memory_store``) serves **every** read, so the
  interactive request path never waits on a SQL warehouse.
* Writers mutate the cache and record the changed key as dirty. A background
  loop flushes only the dirty keys to the ``workbench_state`` Delta table every
  ``_FLUSH_INTERVAL_SECONDS`` (MERGE for present keys, DELETE for removed ones),
  and a final flush runs at shutdown.
* Startup loads every cached store from Delta back into memory.
* Writes that must never be silently lost (app settings, hiding a run from
  history) flush their key synchronously and fail closed when storage is not
  configured.
* Create Agent sessions are a *lazy* store: they are not bulk-loaded at
  startup; a cache miss point-reads Delta (see ``kv_get_durable``).
* Until an admin completes the Setup wizard (no catalog/schema yet), the store
  runs in memory only; state is then ephemeral across restarts.

The app is a single Databricks Apps container; if it were ever scaled out,
each replica would hold its own cache and writes would be last-writer-wins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Optional

from backend.services import app_config, delta_store

logger = logging.getLogger(__name__)

_FLUSH_INTERVAL_SECONDS = 15
_SCHEMA_RETRY_SECONDS = 30
_HISTORY_LIMIT = 30  # scan-history entries kept per space

# In-memory cache — authoritative for reads.
_memory_store: dict = {
    "scans": {},      # space_id -> latest ScanResult dict
    "history": {},    # space_id -> list of score summaries (ordered by scanned_at)
    "stars": set(),   # set of starred space_ids
    "seen": set(),    # set of seen space_ids
    "optimization_runs": {},  # space_id -> latest optimization run dict
    "hidden_runs": {},        # run_id -> {space_id, hidden_by, hidden_at}
    "join_advice": {},        # space_id -> {seeds, updated_at, seeded_by}
    "app_settings": {},       # key -> {value, updated_by, updated_at}
    # ── GenieWatch caches (read-only observability surface) ──
    "watch_space_cache": {},        # space_id -> dict
    "watch_conversation_cache": {}, # (space_id, conversation_id) -> dict
    "watch_message_cache": {},      # (space_id, conversation_id, message_id) -> dict
    "watch_sync_watermark": {},     # resource -> dict
    "watch_daily_rollup": {},       # (space_id, day) -> dict
    # ── Cross-workspace ──
    "workspace_connections": {},    # workspace_id -> dict (secret-scope ref only)
    "workspace_transfer": {},       # (workspace_id, day, category) -> {bytes, requests}
}

# Delta store name -> (cache key, shape). Shapes:
#   "set"   — set of ids                   -> rows (id, "1")
#   "dict"  — str-keyed dict of JSON values -> rows (key, json(value))
#   "tuple" — tuple-keyed dict              -> rows (json(list(key)), json(value))
_STORES: dict[str, tuple[str, str]] = {
    "scans": ("scans", "dict"),
    "scan_history": ("history", "dict"),
    "stars": ("stars", "set"),
    "seen": ("seen", "set"),
    "optimization_runs": ("optimization_runs", "dict"),
    "hidden_runs": ("hidden_runs", "dict"),
    "join_advice": ("join_advice", "dict"),
    "app_settings": ("app_settings", "dict"),
    "watch_space": ("watch_space_cache", "dict"),
    "watch_conversation": ("watch_conversation_cache", "tuple"),
    "watch_message": ("watch_message_cache", "tuple"),
    "watch_watermark": ("watch_sync_watermark", "dict"),
    "watch_rollup": ("watch_daily_rollup", "tuple"),
    "workspace_connections": ("workspace_connections", "dict"),
    "workspace_transfer": ("workspace_transfer", "tuple"),
}

# Lazily-read key/value stores (not bulk-loaded, e.g. Create Agent sessions).
# Callers keep their own cache; this only holds writes not yet flushed:
# store -> {key: serialized JSON, or None for a delete}.
_lazy_pending: dict[str, dict[str, str | None]] = {}

# store -> set of cache keys changed since the last flush
_dirty: dict[str, set] = {}
_flush_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None
_ready = False
_last_error: str | None = None
_retry_after: float = 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── Serialization ────────────────────────────────────────────────────────


def _encode_key(shape: str, key) -> str:
    return json.dumps(list(key), default=str) if shape == "tuple" else str(key)


def _decode_key(shape: str, raw: str):
    return tuple(json.loads(raw)) if shape == "tuple" else raw


def _load_rows(store: str, rows: list[tuple[str, str]]) -> None:
    mem_key, shape = _STORES[store]
    if shape == "set":
        _memory_store[mem_key] = {k for k, _ in rows}
        return
    loaded = {}
    for k, v in rows:
        if not v:
            continue
        try:
            loaded[_decode_key(shape, k)] = json.loads(v)
        except (ValueError, TypeError):
            logger.warning("Skipping unreadable state row %s/%s", store, k[:64])
    _memory_store[mem_key] = loaded


def _mark(store: str, key) -> None:
    _dirty.setdefault(store, set()).add(key)


def _snapshot(store: str, keys: set) -> tuple[list[tuple[str, str]], list[str]]:
    """Split dirty cached-store keys into (rows to upsert, encoded keys to delete)."""
    mem_key, shape = _STORES[store]
    data = _memory_store[mem_key]
    upserts, deletes = [], []
    for key in keys:
        enc = _encode_key(shape, key)
        if shape == "set":
            (upserts.append((enc, "1")) if key in data else deletes.append(enc))
        elif key in data:
            upserts.append((enc, json.dumps(data[key], default=str)))
        else:
            deletes.append(enc)
    return upserts, deletes


# ── Flushing ─────────────────────────────────────────────────────────────


async def _flush_keys(store: str, keys: set) -> None:
    upserts, deletes = _snapshot(store, keys)
    if upserts:
        await asyncio.to_thread(delta_store.upsert, store, upserts)
    if deletes:
        await asyncio.to_thread(delta_store.delete, store, deletes)


async def _flush_lazy(store: str, pending: dict[str, str | None]) -> None:
    upserts = [(k, v) for k, v in pending.items() if v is not None]
    deletes = [k for k, v in pending.items() if v is None]
    if upserts:
        await asyncio.to_thread(delta_store.upsert, store, upserts)
    if deletes:
        await asyncio.to_thread(delta_store.delete, store, deletes)


async def flush() -> None:
    """Write every pending change to Delta. Failures are retried next tick."""
    global _last_error
    if not _ready:
        return
    async with _flush_lock:
        pending = {s: set(k) for s, k in _dirty.items() if k}
        _dirty.clear()
        for store, keys in pending.items():
            try:
                await _flush_keys(store, keys)
            except Exception as e:  # noqa: BLE001 - retry next tick
                _last_error = str(e)
                logger.warning("Delta flush failed for '%s'; will retry", store, exc_info=True)
                _dirty.setdefault(store, set()).update(keys)
        lazy = {s: p for s, p in _lazy_pending.items() if p}
        for store in lazy:
            _lazy_pending[store] = {}
        for store, items in lazy.items():
            try:
                await _flush_lazy(store, items)
            except Exception as e:  # noqa: BLE001 - re-queue without clobbering newer writes
                _last_error = str(e)
                logger.warning("Delta flush failed for '%s'; will retry", store, exc_info=True)
                newer = _lazy_pending.setdefault(store, {})
                for k, v in items.items():
                    newer.setdefault(k, v)


async def _flush_now(store: str, key) -> None:
    """Synchronously persist one key (raises on failure)."""
    async with _flush_lock:
        keys = {key}
        if store in _dirty:
            _dirty[store].discard(key)
        await _flush_keys(store, keys)


async def _flush_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
            await flush()
        except asyncio.CancelledError:
            break
        except Exception:  # pragma: no cover - keep the loop alive
            logger.debug("Delta flush loop error", exc_info=True)


# ── Lifecycle ────────────────────────────────────────────────────────────


async def _ensure_and_load() -> None:
    global _ready, _last_error
    await asyncio.to_thread(delta_store.ensure_table)
    # One statement per store keeps each inline result well under the
    # Statement Execution API's 25 MiB inline limit as caches grow.
    data: dict[str, list[tuple[str, str]]] = {}
    for store in _STORES:
        data.update(await asyncio.to_thread(delta_store.load_stores, [store]))
    # Keep anything written in memory before storage came up (e.g. scans run
    # while setup was pending): merge it over the loaded rows and mark dirty.
    for store, (mem_key, shape) in _STORES.items():
        early = _memory_store[mem_key]
        _load_rows(store, data.get(store, []))
        if early:
            if shape == "set":
                _memory_store[mem_key] |= early
            else:
                _memory_store[mem_key].update(early)
            for key in early:
                _mark(store, key)
    _ready = True
    _last_error = None
    logger.info("Delta state store ready at %s", delta_store.table_name())


def _start_loop() -> None:
    global _flush_task
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.create_task(_flush_loop())


async def init() -> None:
    """Startup: resolve the storage location, create the table, load state."""
    global _last_error, _retry_after
    await asyncio.to_thread(app_config.load)
    if not delta_store.available():
        logger.info(
            "Workbench storage not configured yet (Setup wizard pending) — "
            "running in memory; state is ephemeral until setup completes."
        )
        return
    try:
        await _ensure_and_load()
    except Exception as e:  # noqa: BLE001 - degrade to memory, retry later
        _last_error = str(e)
        _retry_after = time.monotonic() + _SCHEMA_RETRY_SECONDS
        logger.warning("Delta state store init failed: %s. Using memory until it recovers.", e)
        return
    _start_loop()


async def reconfigure() -> None:
    """Bring storage up after the Setup wizard sets a location. Raises on failure."""
    global _ready
    _ready = False
    await _ensure_and_load()
    _start_loop()


async def close() -> None:
    """Stop the flush loop and write anything pending."""
    global _flush_task
    if _flush_task:
        _flush_task.cancel()
        _flush_task = None
    try:
        await flush()
    except Exception:  # noqa: BLE001
        logger.warning("Final Delta flush failed", exc_info=True)


async def _maybe_retry() -> None:
    """If storage is configured but not up yet, retry (rate-limited)."""
    global _retry_after, _last_error
    if _ready or not delta_store.available() or time.monotonic() < _retry_after:
        return
    _retry_after = time.monotonic() + _SCHEMA_RETRY_SECONDS
    try:
        await _ensure_and_load()
        _start_loop()
    except Exception as e:  # noqa: BLE001
        _last_error = str(e)
        logger.debug("Delta state store retry failed", exc_info=True)


def is_available() -> bool:
    """Whether durable Delta storage is configured and loaded."""
    return _ready and delta_store.available()


def is_configured() -> bool:
    """Whether a storage location has been chosen (usable or not)."""
    return app_config.is_configured()


def status() -> dict:
    loc = app_config.location()
    return {
        "configured": loc is not None,
        "available": is_available(),
        "catalog": loc[0] if loc else None,
        "schema": loc[1] if loc else None,
        "source": app_config.source(),
        "last_error": _last_error,
        "pending_writes": sum(len(k) for k in _dirty.values())
        + sum(len(p) for p in _lazy_pending.values()),
    }


# ── Generic key/value (lazy stores, e.g. Create Agent sessions) ──────────


async def kv_put(store: str, key: str, value: Any) -> None:
    """Queue a value for durable write (flushed with the next tick)."""
    _lazy_pending.setdefault(store, {})[key] = json.dumps(value, default=str)


async def kv_get_durable(store: str, key: str) -> Any | None:
    """Read a value: unflushed write first, else a Delta point read (None if absent)."""
    pending = _lazy_pending.get(store, {})
    if key in pending:
        raw = pending[key]
    elif not is_available():
        return None
    else:
        try:
            raw = await asyncio.to_thread(delta_store.get_value, store, key)
        except Exception:  # noqa: BLE001 - fail open (treated as absent)
            logger.warning("Delta point read failed for %s/%s", store, key[:32], exc_info=True)
            return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def kv_delete(store: str, key: str) -> None:
    """Queue a durable delete."""
    _lazy_pending.setdefault(store, {})[key] = None


# ── Scan results / stars / seen / optimization runs ──────────────────────


async def save_scan_result(space_id: str, scan_result: dict) -> None:
    scan_result["scanned_at"] = scan_result.get("scanned_at") or _now()
    _memory_store["scans"][space_id] = scan_result
    history = _memory_store["history"].setdefault(space_id, [])
    history.append({
        "score": scan_result.get("score"),
        "maturity": scan_result.get("maturity"),
        "optimization_accuracy": scan_result.get("optimization_accuracy"),
        "scanned_at": scan_result["scanned_at"],
    })
    _memory_store["history"][space_id] = history[-_HISTORY_LIMIT:]
    _memory_store["seen"].add(space_id)
    _mark("scans", space_id)
    _mark("scan_history", space_id)
    _mark("seen", space_id)


async def get_latest_score(space_id: str) -> Optional[dict]:
    return _memory_store["scans"].get(space_id)


async def get_latest_scores_batch(space_ids: list[str]) -> dict[str, dict]:
    if not space_ids:
        return {}
    scans = _memory_store["scans"]
    return {sid: scans[sid] for sid in space_ids if sid in scans}


def _parse_ts(value) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


async def get_score_history(space_id: str, days: int = 30) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=max(1, int(days)))
    out = []
    for entry in _memory_store["history"].get(space_id, []):
        ts = _parse_ts(entry.get("scanned_at"))
        if ts is not None and ts < cutoff:
            continue
        out.append({
            "score": entry.get("score"),
            "maturity": entry.get("maturity"),
            "optimization_accuracy": entry.get("optimization_accuracy"),
            "scanned_at": entry.get("scanned_at"),
        })
    return out


async def star_space(space_id: str, starred: bool) -> None:
    if starred:
        _memory_store["stars"].add(space_id)
    else:
        _memory_store["stars"].discard(space_id)
    _mark("stars", space_id)


async def get_starred_spaces() -> list[str]:
    await _maybe_retry()
    return list(_memory_store["stars"])


async def is_space_starred(space_id: str) -> bool:
    return space_id in _memory_store["stars"]


async def record_space_seen(space_id: str) -> None:
    if space_id not in _memory_store["seen"]:
        _memory_store["seen"].add(space_id)
        _mark("seen", space_id)


async def get_all_scan_summaries() -> list[dict]:
    await _maybe_retry()
    return [
        {
            "space_id": sid,
            "score": s.get("score"),
            "maturity": s.get("maturity"),
            "findings": s.get("findings", []),
            "scanned_at": s.get("scanned_at"),
        }
        for sid, s in _memory_store["scans"].items()
    ]


async def save_optimization_run(space_id: str, benchmark_total: int, benchmark_correct: int) -> None:
    accuracy = benchmark_correct / benchmark_total if benchmark_total > 0 else 0.0
    _memory_store["optimization_runs"][space_id] = {
        "space_id": space_id,
        "benchmark_total": benchmark_total,
        "benchmark_correct": benchmark_correct,
        "accuracy": accuracy,
        "created_at": _now(),
    }
    _mark("optimization_runs", space_id)


async def get_latest_optimization_run(space_id: str) -> Optional[dict]:
    return _memory_store["optimization_runs"].get(space_id)


async def hide_optimization_run_from_history(run_id: str, space_id: str, hidden_by: str) -> None:
    """Persist a Workbench-only tombstone for a GSO run (fails closed, durable on return)."""
    await _maybe_retry()
    if not is_available():
        raise RuntimeError("Workbench storage is unavailable; history removal was not persisted.")
    _memory_store["hidden_runs"][run_id] = {
        "space_id": space_id,
        "hidden_by": hidden_by or "unknown",
        "hidden_at": _now(),
    }
    try:
        await _flush_now("hidden_runs", run_id)
    except Exception:
        _memory_store["hidden_runs"].pop(run_id, None)
        raise


async def get_hidden_optimization_run_ids(space_id: str) -> set[str]:
    await _maybe_retry()
    return {
        run_id
        for run_id, rec in _memory_store["hidden_runs"].items()
        if rec.get("space_id") == space_id
    }


# ── Join Advisor advice ──────────────────────────────────────────────────


async def save_join_advice(space_id: str, seeds: list[dict], seeded_by: str | None = None) -> dict:
    record = {"seeds": seeds, "seeded_by": seeded_by, "updated_at": _now()}
    if seeds:
        _memory_store["join_advice"][space_id] = record
    else:
        _memory_store["join_advice"].pop(space_id, None)
    _mark("join_advice", space_id)
    return record


async def get_join_advice(space_id: str) -> Optional[dict]:
    return _memory_store["join_advice"].get(space_id)


# ── App-wide settings ────────────────────────────────────────────────────
# Durable only: a setting that silently resets on restart is worse than none,
# so set_app_setting reports False (nothing written) without durable storage.


async def get_app_setting(key: str) -> Optional[str]:
    await _maybe_retry()
    if not is_available():
        return None
    rec = _memory_store["app_settings"].get(key)
    return rec.get("value") if rec else None


async def set_app_setting(key: str, value: str, updated_by: str | None) -> bool:
    await _maybe_retry()
    if not is_available():
        return False
    previous = _memory_store["app_settings"].get(key)
    _memory_store["app_settings"][key] = {"value": value, "updated_by": updated_by, "updated_at": _now()}
    try:
        await _flush_now("app_settings", key)
    except Exception:
        if previous is None:
            _memory_store["app_settings"].pop(key, None)
        else:
            _memory_store["app_settings"][key] = previous
        raise
    return True


# ── GenieWatch caches ────────────────────────────────────────────────────


async def watch_upsert_space(space: dict) -> None:
    space_id = space["space_id"]
    now = _now()
    existing = _memory_store["watch_space_cache"].get(space_id, {})
    _memory_store["watch_space_cache"][space_id] = {
        **existing,
        **space,
        "permissions": space.get("permissions") or [],
        "last_seen_at": now,
        "updated_at": now,
    }
    _mark("watch_space", space_id)


async def watch_list_cached_spaces() -> list[dict]:
    await _maybe_retry()
    rows = [
        {**s, "permissions": s.get("permissions") or []}
        for s in _memory_store["watch_space_cache"].values()
    ]
    return sorted(rows, key=lambda s: s.get("last_seen_at") or "", reverse=True)


async def watch_upsert_conversation(conv: dict) -> None:
    key = (conv["space_id"], conv["conversation_id"])
    _memory_store["watch_conversation_cache"][key] = conv
    _mark("watch_conversation", key)


async def watch_list_conversations(space_id: str, limit: int = 100) -> list[dict]:
    out = [c for k, c in _memory_store["watch_conversation_cache"].items() if k[0] == space_id]
    return sorted(out, key=lambda c: c.get("last_message_at") or "", reverse=True)[:limit]


async def watch_upsert_message(msg: dict) -> None:
    key = (msg["space_id"], msg["conversation_id"], msg["message_id"])
    _memory_store["watch_message_cache"][key] = msg
    _mark("watch_message", key)


async def watch_get_watermark(resource: str) -> Optional[dict]:
    return _memory_store["watch_sync_watermark"].get(resource)


async def watch_set_watermark(resource: str, status: str, error: str | None = None) -> None:
    _memory_store["watch_sync_watermark"][resource] = {
        "resource": resource,
        "last_synced_at": _now(),
        "status": status,
        "error": error,
    }
    _mark("watch_watermark", resource)


async def watch_upsert_daily_rollup(row: dict) -> None:
    key = (row["space_id"], str(row["day"]))
    _memory_store["watch_daily_rollup"][key] = {**row, "day": str(row["day"])}
    _mark("watch_rollup", key)


# ── Cross-workspace connections (secret-scope reference only) ────────────


async def list_workspace_connections() -> list[dict]:
    await _maybe_retry()
    return [dict(c) for c in _memory_store["workspace_connections"].values()]


async def get_workspace_connection(workspace_id: str) -> Optional[dict]:
    await _maybe_retry()
    row = _memory_store["workspace_connections"].get(workspace_id)
    return dict(row) if row else None


async def upsert_workspace_connection(conn: dict) -> None:
    now = _now()
    existing = _memory_store["workspace_connections"].get(conn["workspace_id"], {})
    _memory_store["workspace_connections"][conn["workspace_id"]] = {
        "created_at": existing.get("created_at", now),
        **existing,
        **conn,
        "updated_at": now,
    }
    _mark("workspace_connections", conn["workspace_id"])


async def update_workspace_connection_status(
    workspace_id: str, status: str, status_detail: str | None = None
) -> None:
    row = _memory_store["workspace_connections"].get(workspace_id)
    if row:
        row.update(status=status, status_detail=status_detail, updated_at=_now())
        _mark("workspace_connections", workspace_id)


async def delete_workspace_connection(workspace_id: str) -> None:
    if _memory_store["workspace_connections"].pop(workspace_id, None) is not None:
        _mark("workspace_connections", workspace_id)


# ── Cross-workspace data-transfer metering ───────────────────────────────


def _to_date(day) -> date:
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    return date.fromisoformat(str(day)[:10])


async def add_workspace_transfer(rows: list[dict]) -> None:
    store = _memory_store["workspace_transfer"]
    for row in rows or []:
        key = (row["workspace_id"], _to_date(row["day"]).isoformat(), row["category"])
        slot = store.setdefault(key, {"bytes": 0, "requests": 0})
        slot["bytes"] += int(row.get("bytes") or 0)
        slot["requests"] += int(row.get("requests") or 0)
        _mark("workspace_transfer", key)


async def list_workspace_transfer(days: int = 30) -> list[dict]:
    cutoff = date.today() - timedelta(days=max(1, int(days)) - 1)
    out = []
    for (ws, day_iso, cat), slot in _memory_store["workspace_transfer"].items():
        try:
            if _to_date(day_iso) < cutoff:
                continue
        except ValueError:
            continue
        out.append({
            "workspace_id": ws,
            "day": day_iso,
            "category": cat,
            "bytes": int(slot.get("bytes") or 0),
            "requests": int(slot.get("requests") or 0),
        })
    return out
