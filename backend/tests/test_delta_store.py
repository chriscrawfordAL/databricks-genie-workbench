"""SQL layer of the Workbench Delta state table (``services.delta_store``)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.service.sql import StatementState

from backend.services import app_config, delta_store


# ── Quoting ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("plain", "'plain'"),
        ("it's", "'it\\'s'"),
        ("a\\b", "'a\\\\b'"),
        ("\\'", "'\\\\\\''"),
        (None, "NULL"),
        (42, "'42'"),
    ],
)
def test_lit_escapes_quotes_and_backslashes(value, expected):
    assert delta_store.lit(value) == expected


def test_lit_cannot_be_broken_out_of():
    rendered = delta_store.lit("x' OR '1'='1")
    # Every quote inside the literal is escaped, so only the outer pair is bare.
    inner = rendered[1:-1]
    assert "'" not in inner.replace("\\'", "")


def test_quote_ident_doubles_backticks():
    assert delta_store.quote_ident("my-catalog") == "`my-catalog`"
    assert delta_store.quote_ident("we`ird") == "`we``ird`"


# ── Location ─────────────────────────────────────────────────────────────


def test_table_name_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(app_config, "location", lambda: None)
    with pytest.raises(delta_store.DeltaStoreNotConfigured):
        delta_store.table_name()


def test_table_name_quotes_the_location(monkeypatch):
    monkeypatch.setattr(app_config, "location", lambda: ("main", "genie_state"))
    assert delta_store.table_name() == "`main`.`genie_state`.workbench_state"


def test_available_needs_a_location_and_a_warehouse(monkeypatch):
    monkeypatch.setattr(app_config, "location", lambda: ("main", "s"))
    monkeypatch.setattr(delta_store, "warehouse_id", lambda: "")
    assert delta_store.available() is False
    monkeypatch.setattr(delta_store, "warehouse_id", lambda: "wh")
    assert delta_store.available() is True
    monkeypatch.setattr(app_config, "location", lambda: None)
    assert delta_store.available() is False


# ── Writes ───────────────────────────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr(app_config, "location", lambda: ("c", "s"))
    monkeypatch.setattr(
        delta_store, "run_statement", lambda sql, **_k: statements.append(sql) or [],
    )
    return statements


def test_upsert_is_one_merge_keyed_on_store_and_key(captured):
    delta_store.upsert("stars", [("a", "1"), ("b", "1")])
    [sql] = captured
    assert sql.startswith("MERGE INTO `c`.`s`.workbench_state t")
    assert "VALUES ('a', '1'), ('b', '1')" in sql
    assert "ON t.store = 'stars' AND t.k = s.k" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql


def test_upsert_batches_by_statement_size(captured, monkeypatch):
    monkeypatch.setattr(delta_store, "_MAX_STATEMENT_BYTES", 120)
    items = [(f"k{i}", "x" * 40) for i in range(5)]
    delta_store.upsert("scans", items)
    assert len(captured) == 3  # ~58 bytes per row: 2 + 2 + 1 rows under a 120-byte budget
    for sql in captured:
        assert sql.startswith("MERGE INTO")
    assert sum(sql.count("'k") for sql in captured) == 5


def test_upsert_of_nothing_runs_nothing(captured):
    delta_store.upsert("stars", [])
    assert captured == []


def test_delete_chunks_keys(captured):
    delta_store.delete("seen", [f"k{i}" for i in range(2500)])
    assert len(captured) == 3
    assert all(sql.startswith("DELETE FROM `c`.`s`.workbench_state WHERE store = 'seen'") for sql in captured)


def test_ensure_table_creates_the_clustered_kv_table(captured):
    delta_store.ensure_table()
    [sql] = captured
    assert sql.startswith("CREATE TABLE IF NOT EXISTS `c`.`s`.workbench_state")
    assert "CLUSTER BY (store, k)" in sql


def test_load_stores_groups_rows(monkeypatch):
    monkeypatch.setattr(app_config, "location", lambda: ("c", "s"))
    monkeypatch.setattr(delta_store, "run_statement", lambda sql, **_k: [
        {"store": "stars", "k": "a", "v": "1"},
        {"store": "scans", "k": "s1", "v": "{}"},
    ])
    out = delta_store.load_stores(["stars", "scans", "seen"])
    assert out == {"stars": [("a", "1")], "scans": [("s1", "{}")], "seen": []}


def test_get_value_point_reads(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(app_config, "location", lambda: ("c", "s"))

    def run(sql, **_k):
        seen.append(sql)
        return [{"v": '{"a": 1}'}]

    monkeypatch.setattr(delta_store, "run_statement", run)
    assert delta_store.get_value("agent_sessions", "s'1") == '{"a": 1}'
    assert "k = 's\\'1'" in seen[0] and "LIMIT 1" in seen[0]


# ── Statement execution ──────────────────────────────────────────────────


def _resp(state, *, statement_id="st-1", cols=("k",), data=None, next_chunk=None, error=None):
    return SimpleNamespace(
        statement_id=statement_id,
        status=SimpleNamespace(state=state, error=error),
        manifest=SimpleNamespace(
            schema=SimpleNamespace(columns=[SimpleNamespace(name=c) for c in cols]),
        ),
        result=SimpleNamespace(data_array=data, next_chunk_index=next_chunk),
    )


def test_run_statement_polls_until_done_and_follows_chunks(monkeypatch):
    monkeypatch.setattr(delta_store.time, "sleep", lambda _s: None)
    ws = MagicMock()
    ws.statement_execution.execute_statement.return_value = _resp(StatementState.PENDING, data=None)
    ws.statement_execution.get_statement.side_effect = [
        _resp(StatementState.RUNNING),
        _resp(StatementState.SUCCEEDED, data=[["a"], ["b"]], next_chunk=1),
    ]
    ws.statement_execution.get_statement_result_chunk_n.side_effect = [
        SimpleNamespace(data_array=[["c"]], next_chunk_index=2),
        SimpleNamespace(data_array=[["d"]], next_chunk_index=None),
    ]

    rows = delta_store.run_statement("SELECT k", client=ws, warehouse="wh")

    assert rows == [{"k": "a"}, {"k": "b"}, {"k": "c"}, {"k": "d"}]
    assert ws.statement_execution.get_statement.call_count == 2
    chunk_calls = ws.statement_execution.get_statement_result_chunk_n.call_args_list
    assert [c.args for c in chunk_calls] == [("st-1", 1), ("st-1", 2)]


def test_run_statement_raises_on_failure(monkeypatch):
    ws = MagicMock()
    ws.statement_execution.execute_statement.return_value = _resp(
        StatementState.FAILED, error=SimpleNamespace(message="TABLE_OR_VIEW_NOT_FOUND"),
    )
    with pytest.raises(RuntimeError, match="TABLE_OR_VIEW_NOT_FOUND"):
        delta_store.run_statement("SELECT 1", client=ws, warehouse="wh")


def test_run_statement_times_out_and_cancels(monkeypatch):
    clock = iter([0.0, 1000.0, 2000.0])
    monkeypatch.setattr(delta_store.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(delta_store.time, "sleep", lambda _s: None)
    ws = MagicMock()
    ws.statement_execution.execute_statement.return_value = _resp(StatementState.PENDING)
    with pytest.raises(TimeoutError):
        delta_store.run_statement("SELECT 1", client=ws, warehouse="wh")
    ws.statement_execution.cancel_execution.assert_called_once_with("st-1")


def test_run_statement_requires_a_warehouse(monkeypatch):
    monkeypatch.setattr(delta_store, "warehouse_id", lambda: "")
    with pytest.raises(delta_store.DeltaStoreNotConfigured):
        delta_store.run_statement("SELECT 1", client=MagicMock())
