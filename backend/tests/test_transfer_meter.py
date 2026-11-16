"""Cross-workspace data-transfer metering.

Verifies byte estimation, that only remote (targeted) pulls are counted, and
that buffered counters flush into Lakebase (in-memory fallback here).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.services import auth, state_store, transfer_meter


@pytest.fixture(autouse=True)
def _clean_state():
    transfer_meter._buffer.clear()
    auth.clear_target_workspace()
    state_store._memory_store["workspace_transfer"].clear()
    yield
    transfer_meter._buffer.clear()
    auth.clear_target_workspace()
    state_store._memory_store["workspace_transfer"].clear()


def test_estimate_bytes():
    assert transfer_meter.estimate_bytes(None) == 0
    assert transfer_meter.estimate_bytes({"a": 1}) > 0
    big = transfer_meter.estimate_bytes([{"x": "y" * 100}])
    small = transfer_meter.estimate_bytes([{"x": "y"}])
    assert big > small


def test_record_current_noop_without_target():
    # No target selected → home-local traffic → not counted.
    transfer_meter.record_current(transfer_meter.CATEGORY_GENIE, {"serialized_space": "x" * 1000})
    assert transfer_meter._snapshot_and_clear() == []


def test_record_current_counts_remote_target():
    auth.set_target_workspace("ws_remote", MagicMock())
    transfer_meter.record_current(transfer_meter.CATEGORY_SQL, [[1, 2, 3], [4, 5, 6]])
    rows = transfer_meter._snapshot_and_clear()
    assert len(rows) == 1
    assert rows[0]["workspace_id"] == "ws_remote"
    assert rows[0]["category"] == "sql"
    assert rows[0]["bytes"] > 0
    assert rows[0]["requests"] == 1


def test_record_accumulates_same_bucket():
    transfer_meter.record("ws_a", transfer_meter.CATEGORY_GENIE, 100)
    transfer_meter.record("ws_a", transfer_meter.CATEGORY_GENIE, 50)
    rows = transfer_meter._snapshot_and_clear()
    assert len(rows) == 1
    assert rows[0]["bytes"] == 150
    assert rows[0]["requests"] == 2


def test_record_ignores_zero_and_empty_workspace():
    transfer_meter.record("", transfer_meter.CATEGORY_GENIE, 100)
    transfer_meter.record("ws_a", transfer_meter.CATEGORY_GENIE, 0)
    assert transfer_meter._snapshot_and_clear() == []


def test_unknown_category_bucketed_as_other():
    transfer_meter.record("ws_a", "made_up", 10)
    rows = transfer_meter._snapshot_and_clear()
    assert rows[0]["category"] == "other"


async def test_flush_persists_and_clears(monkeypatch):
    # Force the Lakebase in-memory fallback path.
    monkeypatch.setattr(state_store, "is_available", lambda: False)
    transfer_meter.record("ws_a", transfer_meter.CATEGORY_SYSTEM_TABLES, 500)

    await transfer_meter.flush()

    # Buffer drained.
    assert transfer_meter._snapshot_and_clear() == []
    # Persisted and readable.
    rows = await state_store.list_workspace_transfer(days=30)
    assert any(r["workspace_id"] == "ws_a" and r["bytes"] == 500 for r in rows)


async def test_flush_rebuffers_on_failure(monkeypatch):
    async def boom(_rows):
        raise RuntimeError("storage down")

    monkeypatch.setattr(state_store, "add_workspace_transfer", boom)
    transfer_meter.record("ws_a", transfer_meter.CATEGORY_GENIE, 42)

    await transfer_meter.flush()  # should not raise

    # Bytes not lost — still buffered for the next attempt.
    rows = transfer_meter._snapshot_and_clear()
    assert len(rows) == 1 and rows[0]["bytes"] == 42
