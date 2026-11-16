"""Workbench state store: in-memory reads, Delta durability (replaces Lakebase).

The Delta side is a fake (``FakeDelta``) patched over ``delta_store`` so these
tests never touch a warehouse.
"""

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.services import app_config, delta_store, state_store


class FakeDelta:
    """In-memory stand-in for the ``workbench_state`` table."""

    def __init__(self):
        self.table: dict[tuple[str, str], str] = {}
        self.upserts: list[tuple[str, list[tuple[str, str]]]] = []
        self.deletes: list[tuple[str, list[str]]] = []
        self.point_reads: list[tuple[str, str]] = []
        self.fail_upserts = 0
        self.ensured = 0

    def upsert(self, store, items):
        if self.fail_upserts:
            self.fail_upserts -= 1
            raise RuntimeError("warehouse down")
        self.upserts.append((store, list(items)))
        for k, v in items:
            self.table[(store, k)] = v

    def delete(self, store, keys):
        self.deletes.append((store, list(keys)))
        for k in keys:
            self.table.pop((store, k), None)

    def ensure_table(self):
        self.ensured += 1

    def load_stores(self, stores):
        out = {s: [] for s in stores}
        for (store, k), v in self.table.items():
            if store in out:
                out[store].append((k, v))
        return out

    def get_value(self, store, key):
        self.point_reads.append((store, key))
        return self.table.get((store, key))


def _empty_memory() -> dict:
    return {
        key: (set() if isinstance(value, set) else {})
        for key, value in state_store._memory_store.items()
    }


@pytest.fixture
def fake(monkeypatch):
    """Fresh state-store globals + a fake Delta table; storage NOT available."""
    delta = FakeDelta()
    monkeypatch.setattr(state_store, "_memory_store", _empty_memory())
    monkeypatch.setattr(state_store, "_dirty", {})
    monkeypatch.setattr(state_store, "_lazy_pending", {})
    monkeypatch.setattr(state_store, "_ready", False)
    monkeypatch.setattr(state_store, "_last_error", None)
    monkeypatch.setattr(state_store, "_retry_after", 0)
    monkeypatch.setattr(state_store, "_flush_task", None)
    monkeypatch.setattr(state_store, "_flush_lock", asyncio.Lock())
    for name in ("upsert", "delete", "ensure_table", "load_stores", "get_value"):
        monkeypatch.setattr(delta_store, name, getattr(delta, name))
    monkeypatch.setattr(delta_store, "available", lambda: False)
    monkeypatch.setattr(delta_store, "table_name", lambda: "`c`.`s`.workbench_state")
    monkeypatch.setattr(app_config, "location", lambda: None)
    return delta


@pytest.fixture
def durable(fake, monkeypatch):
    """Storage configured and loaded."""
    monkeypatch.setattr(delta_store, "available", lambda: True)
    monkeypatch.setattr(app_config, "location", lambda: ("c", "s"))
    monkeypatch.setattr(state_store, "_ready", True)
    return fake


def _is_utc(value: str) -> bool:
    return datetime.fromisoformat(value).utcoffset() == timedelta(0)


# ── App settings: durable only ───────────────────────────────────────────


async def test_unset_setting_reads_none(durable):
    assert await state_store.get_app_setting("release_check") is None


async def test_without_durable_storage_settings_are_not_stored(fake):
    assert state_store.is_available() is False
    assert await state_store.set_app_setting("release_check", "on", "a@b.c") is False
    assert await state_store.get_app_setting("release_check") is None
    assert state_store._memory_store["app_settings"] == {}
    assert fake.upserts == []


async def test_set_then_get_round_trips_and_is_durable_on_return(durable):
    assert await state_store.set_app_setting("release_check", "on", "a@b.c") is True
    stored = json.loads(durable.table[("app_settings", "release_check")])
    assert stored["value"] == "on"
    assert stored["updated_by"] == "a@b.c"
    assert await state_store.get_app_setting("release_check") == "on"
    # Written synchronously, not left for the flush loop.
    assert "release_check" not in state_store._dirty.get("app_settings", set())


async def test_a_failed_setting_write_raises_and_keeps_the_old_value(durable):
    await state_store.set_app_setting("release_check", "off", "a@b.c")
    durable.fail_upserts = 1
    with pytest.raises(RuntimeError):
        await state_store.set_app_setting("release_check", "on", "a@b.c")
    assert await state_store.get_app_setting("release_check") == "off"


def test_is_configured_follows_the_storage_location(fake, monkeypatch):
    assert state_store.is_configured() is False
    monkeypatch.setattr(app_config, "location", lambda: ("c", "s"))
    assert state_store.is_configured() is True


# ── UTC timestamps ───────────────────────────────────────────────────────


async def test_a_scan_saved_without_a_time_is_stamped_in_utc(fake):
    await state_store.save_scan_result("s1", {"score": 1, "maturity": "Not Ready"})
    assert _is_utc(state_store._memory_store["scans"]["s1"]["scanned_at"])


async def test_an_optimization_run_is_stamped_in_utc(fake):
    await state_store.save_optimization_run("s1", 10, 5)
    assert _is_utc((await state_store.get_latest_optimization_run("s1"))["created_at"])


async def test_join_advice_returns_a_utc_time(fake):
    record = await state_store.save_join_advice("s1", [{"left": "a", "right": "b"}], seeded_by="u")
    assert _is_utc(record["updated_at"])


async def test_watch_writes_are_stamped_in_utc(fake):
    await state_store.watch_upsert_space({"space_id": "s1", "title": "t"})
    await state_store.watch_set_watermark("spaces", "ok")
    assert _is_utc(state_store._memory_store["watch_space_cache"]["s1"]["updated_at"])
    assert _is_utc((await state_store.watch_get_watermark("spaces"))["last_synced_at"])


def test_state_store_never_calls_utcnow():
    assert "utcnow" not in inspect.getsource(state_store)


# ── Dirty-key flushing ───────────────────────────────────────────────────


async def test_flush_merges_changed_keys_and_deletes_removed_ones(durable):
    await state_store.star_space("s1", True)
    await state_store.save_scan_result("s1", {"score": 7, "maturity": "Trusted"})
    await state_store.flush()

    assert ("stars", "s1") in durable.table
    assert json.loads(durable.table[("scans", "s1")])["score"] == 7
    assert state_store._dirty == {}

    await state_store.star_space("s1", False)
    await state_store.flush()
    assert ("stars", ["s1"]) in durable.deletes
    assert ("stars", "s1") not in durable.table


async def test_flush_only_writes_dirty_keys(durable):
    await state_store.star_space("a", True)
    await state_store.star_space("b", True)
    await state_store.flush()
    durable.upserts.clear()

    await state_store.star_space("c", True)
    await state_store.flush()
    assert durable.upserts == [("stars", [("c", "1")])]


async def test_nothing_flushes_before_storage_is_ready(fake):
    await state_store.star_space("s1", True)
    await state_store.flush()
    assert fake.upserts == []
    assert "s1" in state_store._dirty["stars"]


async def test_a_failed_flush_is_requeued_and_retried(durable):
    await state_store.star_space("s1", True)
    durable.fail_upserts = 1
    await state_store.flush()
    assert "s1" in state_store._dirty["stars"]
    assert state_store.status()["last_error"]

    await state_store.flush()
    assert ("stars", "s1") in durable.table
    assert state_store._dirty == {}


async def test_tuple_keyed_stores_round_trip_through_delta(durable):
    conv = {"space_id": "sp", "conversation_id": "c1", "message_count": 3}
    await state_store.watch_upsert_conversation(conv)
    await state_store.flush()
    [raw_key] = [k for store, k in durable.table if store == "watch_conversation"]
    assert json.loads(raw_key) == ["sp", "c1"]

    state_store._memory_store["watch_conversation_cache"] = {}
    state_store._load_rows("watch_conversation", durable.load_stores(["watch_conversation"])["watch_conversation"])
    assert await state_store.watch_list_conversations("sp") == [conv]


# ── Hiding a run from history ────────────────────────────────────────────


async def test_hiding_a_run_fails_closed_without_durable_storage(fake):
    with pytest.raises(RuntimeError):
        await state_store.hide_optimization_run_from_history("r1", "sp", "u@x")
    assert state_store._memory_store["hidden_runs"] == {}


async def test_hiding_a_run_is_durable_before_it_returns(durable):
    await state_store.hide_optimization_run_from_history("r1", "sp", "u@x")
    rec = json.loads(durable.table[("hidden_runs", "r1")])
    assert rec["space_id"] == "sp" and rec["hidden_by"] == "u@x"
    assert await state_store.get_hidden_optimization_run_ids("sp") == {"r1"}
    assert await state_store.get_hidden_optimization_run_ids("other") == set()


async def test_a_failed_hide_is_not_reported_as_hidden(durable):
    durable.fail_upserts = 1
    with pytest.raises(RuntimeError):
        await state_store.hide_optimization_run_from_history("r1", "sp", "u@x")
    assert await state_store.get_hidden_optimization_run_ids("sp") == set()


# ── Startup load ─────────────────────────────────────────────────────────


async def test_early_memory_writes_survive_the_startup_load(fake, monkeypatch):
    fake.table[("stars", "old")] = "1"
    fake.table[("scans", "s0")] = json.dumps({"score": 3})
    await state_store.star_space("new", True)  # written while setup was pending

    monkeypatch.setattr(delta_store, "available", lambda: True)
    await state_store._ensure_and_load()

    assert state_store.is_available() is True
    assert fake.ensured == 1
    assert set(await state_store.get_starred_spaces()) == {"old", "new"}
    assert (await state_store.get_latest_score("s0"))["score"] == 3
    assert state_store._dirty["stars"] == {"new"}


async def test_init_stays_in_memory_until_storage_is_configured(fake, monkeypatch):
    monkeypatch.setattr(app_config, "load", lambda: False)
    await state_store.init()
    assert state_store.is_available() is False
    assert fake.ensured == 0


async def test_init_degrades_to_memory_when_the_load_fails(fake, monkeypatch):
    monkeypatch.setattr(app_config, "load", lambda: True)
    monkeypatch.setattr(delta_store, "available", lambda: True)

    def broken():
        raise RuntimeError("no such schema")

    monkeypatch.setattr(delta_store, "ensure_table", broken)
    await state_store.init()
    assert state_store.is_available() is False
    assert "no such schema" in state_store.status()["last_error"]


# ── Lazy key/value (Create Agent sessions) ───────────────────────────────


async def test_lazy_kv_reads_pending_writes_then_point_reads_delta(durable):
    await state_store.kv_put("agent_sessions", "s1", {"n": 1})
    assert await state_store.kv_get_durable("agent_sessions", "s1") == {"n": 1}
    assert durable.point_reads == []

    await state_store.flush()
    assert state_store._lazy_pending["agent_sessions"] == {}
    assert json.loads(durable.table[("agent_sessions", "s1")]) == {"n": 1}

    assert await state_store.kv_get_durable("agent_sessions", "s1") == {"n": 1}
    assert durable.point_reads == [("agent_sessions", "s1")]


async def test_lazy_kv_delete_is_a_tombstone_until_flushed(durable):
    durable.table[("agent_sessions", "s1")] = json.dumps({"n": 1})
    await state_store.kv_delete("agent_sessions", "s1")
    assert await state_store.kv_get_durable("agent_sessions", "s1") is None
    assert durable.point_reads == []

    await state_store.flush()
    assert ("agent_sessions", ["s1"]) in durable.deletes
    assert ("agent_sessions", "s1") not in durable.table


async def test_a_failed_lazy_flush_keeps_newer_writes(durable, monkeypatch):
    await state_store.kv_put("agent_sessions", "s1", {"v": "old"})
    durable.fail_upserts = 1
    real_upsert = durable.upsert

    def upsert_then_newer_write(store, items):
        # A newer write lands while the failing flush is in flight.
        state_store._lazy_pending.setdefault("agent_sessions", {})["s1"] = json.dumps({"v": "new"})
        return real_upsert(store, items)

    monkeypatch.setattr(delta_store, "upsert", upsert_then_newer_write)
    await state_store.flush()
    assert json.loads(state_store._lazy_pending["agent_sessions"]["s1"]) == {"v": "new"}


async def test_lazy_kv_without_storage_reads_nothing_durable(fake):
    assert await state_store.kv_get_durable("agent_sessions", "missing") is None
    assert fake.point_reads == []


# ── Scan history ─────────────────────────────────────────────────────────


async def test_score_history_is_trimmed_to_the_requested_days(fake):
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    recent = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await state_store.save_scan_result("s1", {"score": 4, "maturity": "Not Ready", "scanned_at": old})
    await state_store.save_scan_result("s1", {"score": 9, "maturity": "Trusted", "scanned_at": recent})

    assert [r["score"] for r in await state_store.get_score_history("s1", days=30)] == [9]
    assert [r["score"] for r in await state_store.get_score_history("s1", days=60)] == [4, 9]


async def test_score_history_keeps_only_summary_fields(fake):
    await state_store.save_scan_result("s1", {
        "score": 4, "maturity": "Not Ready", "findings": ["secret"], "checks": [{"x": 1}],
    })
    [row] = state_store._memory_store["history"]["s1"]
    assert set(row) == {"score", "maturity", "optimization_accuracy", "scanned_at"}


async def test_workspace_transfer_accumulates_per_day_and_category(fake):
    today = datetime.now(UTC).date().isoformat()
    row = {"workspace_id": "w1", "day": today, "category": "genie", "bytes": 10, "requests": 1}
    await state_store.add_workspace_transfer([row, row])
    [out] = await state_store.list_workspace_transfer(days=7)
    assert (out["bytes"], out["requests"]) == (20, 2)
