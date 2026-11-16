"""Remote (cross-workspace, separate metastore) agent targeting.

Pins the contract the Workbench app codes against: the ``target`` job param
(:class:`AgentTarget` JSON with a secret *reference*), the process-wide override
that routes every agent-side Genie / SQL / UC-metadata call to the remote
workspace, and the guarantee that LLM calls and GSO state stay home.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
import yaml

from genie_space_optimizer.common import agent_target as at

REPO_ROOT = Path(__file__).resolve().parents[4]
PKG_ROOT = Path(__file__).resolve().parents[2]

TARGET = {
    "workspace_id": "ws_0123456789abcdef",
    "host": "https://remote.example.com",
    "client_id": "remote-sp-id",
    "secret_scope": "genie-workbench-workspaces",
    "secret_key": "ws_0123456789abcdef",
    "warehouse_id": "remote-wh",
    "remote_workspace_id": "999",
}


@pytest.fixture(autouse=True)
def _reset_override():
    at.clear_agent_target()
    yield
    at.clear_agent_target()


def _activate(client=None, warehouse="remote-wh"):
    client = client or MagicMock(name="remote")
    at.set_agent_target(client, warehouse, at.AgentTarget.from_dict(TARGET))
    return client


# ── AgentTarget ──────────────────────────────────────────────────────────


def test_target_json_round_trip_and_never_carries_a_secret():
    t = at.AgentTarget.from_dict(TARGET)
    raw = t.to_json()
    assert at.AgentTarget.from_json(raw) == t
    assert "secret" not in json.loads(raw) or json.loads(raw).keys() >= {"secret_scope", "secret_key"}
    assert "client_secret" not in raw


@pytest.mark.parametrize("raw", [None, "", "  ", "{}"])
def test_empty_target_is_a_home_run(raw):
    assert at.AgentTarget.from_json(raw) is None


def test_target_validation():
    with pytest.raises(ValueError, match="missing required fields"):
        at.AgentTarget.from_dict({**TARGET, "client_id": ""})
    with pytest.raises(ValueError, match="https://"):
        at.AgentTarget.from_dict({**TARGET, "host": "http://remote.example.com"})
    with pytest.raises(ValueError, match="not valid JSON"):
        at.AgentTarget.from_json("{nope")
    t = at.AgentTarget.from_dict({**TARGET, "host": "https://remote.example.com/", "warehouse_id": ""})
    assert t.host == "https://remote.example.com" and t.warehouse_id is None


# ── Registry ─────────────────────────────────────────────────────────────


def test_registry_is_identity_when_inactive():
    home = object()
    assert not at.remote_active()
    assert at.agent_client(home) is home
    assert at.agent_warehouse("home-wh") == "home-wh"
    assert at.agent_sql(home, "home-wh") == (home, "home-wh")
    at.require_no_remote_spark("x")  # no-op


def test_registry_routes_when_active():
    remote = _activate()
    assert at.remote_active()
    assert at.agent_client(object()) is remote
    assert at.agent_warehouse("home-wh") == "remote-wh"
    assert at.agent_sql(object(), "home-wh") == (remote, "remote-wh")
    with pytest.raises(at.RemoteSparkUnavailable):
        at.require_no_remote_spark("x")
    at.clear_agent_target()
    assert not at.remote_active()


def test_registry_is_visible_to_worker_threads():
    import concurrent.futures

    remote = _activate()
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        assert pool.submit(at.agent_client, object()).result() is remote


# ── build_agent_client ───────────────────────────────────────────────────


def _home_with_secret(value="s3cret"):
    home = MagicMock(name="home")
    home.secrets.get_secret.return_value = SimpleNamespace(
        value=base64.b64encode(value.encode()).decode()
    )
    return home


def test_build_agent_client_exchanges_the_scoped_secret(monkeypatch):
    calls = []

    def fake_post(url, auth=None, data=None, headers=None, timeout=None):
        calls.append((url, auth, data))
        return SimpleNamespace(status_code=200, text="", json=lambda: {"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("databricks.sdk.config.get_host_metadata", lambda _h: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setenv("DATABRICKS_WORKSPACE_ID", "111")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "home-sp")
    home = _home_with_secret()
    client = at.build_agent_client(home, at.AgentTarget.from_dict(TARGET))

    home.secrets.get_secret.assert_called_once_with(scope="genie-workbench-workspaces", key="ws_0123456789abcdef")
    assert calls[0][0] == "https://remote.example.com/oidc/v1/token"
    assert calls[0][1] == ("remote-sp-id", "s3cret")
    assert calls[0][2]["grant_type"] == "client_credentials"
    cfg = client.config
    assert cfg.host == "https://remote.example.com"
    assert cfg.workspace_id == "999"  # never the home workspace id
    assert not cfg.client_id
    assert cfg.authenticate() == {"Authorization": "Bearer tok"}


def test_build_agent_client_fails_fast_on_bad_credentials(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: SimpleNamespace(status_code=401, text="invalid_client", json=lambda: {}),
    )
    with pytest.raises(RuntimeError, match="401"):
        at.build_agent_client(_home_with_secret(), at.AgentTarget.from_dict(TARGET))


def test_resolve_agent_warehouse_prefers_configured_then_running():
    client = MagicMock()
    assert at.resolve_agent_warehouse(client, at.AgentTarget.from_dict(TARGET)) == "remote-wh"
    client.warehouses.list.return_value = [
        SimpleNamespace(id="classic", enable_serverless_compute=False, warehouse_type=SimpleNamespace(value="CLASSIC"), state=SimpleNamespace(value="RUNNING")),
        SimpleNamespace(id="stopped-pro", enable_serverless_compute=False, warehouse_type=SimpleNamespace(value="PRO"), state=SimpleNamespace(value="STOPPED")),
        SimpleNamespace(id="running-sl", enable_serverless_compute=True, warehouse_type=SimpleNamespace(value="PRO"), state=SimpleNamespace(value="RUNNING")),
    ]
    t = at.AgentTarget.from_dict({**TARGET, "warehouse_id": ""})
    assert at.resolve_agent_warehouse(client, t) == "running-sl"
    client.warehouses.list.return_value = []
    with pytest.raises(RuntimeError, match="No Pro or Serverless"):
        at.resolve_agent_warehouse(client, t)


def test_configure_from_param(monkeypatch):
    remote = MagicMock(name="remote")
    monkeypatch.setattr(at, "build_agent_client", lambda home, t: remote)
    assert at.configure_agent_target_from_param(MagicMock(), "") is None
    assert not at.remote_active()
    t = at.configure_agent_target_from_param(MagicMock(), json.dumps(TARGET))
    assert t is not None and at.agent_client(None) is remote and at.agent_warehouse(None) == "remote-wh"
    # A later home run clears the override.
    at.configure_agent_target_from_param(MagicMock(), "")
    assert not at.remote_active()


# ── Genie funnel ─────────────────────────────────────────────────────────


def test_genie_funnel_uses_remote_client_only_when_active():
    from genie_space_optimizer.common import genie_client

    home, remote = MagicMock(name="home"), MagicMock(name="remote")
    for c in (home, remote):
        c.api_client.do.return_value = {"access_control_list": []}
    genie_client.get_space_permissions_rest(home, "s1")
    assert home.api_client.do.called and not remote.api_client.do.called
    home.reset_mock()
    _activate(remote)
    genie_client.get_space_permissions_rest(home, "s1")
    assert remote.api_client.do.called and not home.api_client.do.called


def test_eval_runner_uses_remote_genie():
    from genie_space_optimizer.optimization.eval_runner import OfficialBenchmarkRunner

    home, remote = MagicMock(name="home"), MagicMock(name="remote")
    _activate(remote)
    runner = OfficialBenchmarkRunner(home)
    assert runner._genie is remote.genie


# ── Agent-data SQL ───────────────────────────────────────────────────────


class _NoSpark:
    def sql(self, *_a, **_k):
        raise AssertionError("agent SQL must never run on the home Spark session")


def _succeeded(columns=("plan",), rows=(("ok",),)):
    from databricks.sdk.service.sql import StatementState

    return SimpleNamespace(
        status=SimpleNamespace(state=StatementState.SUCCEEDED, error=None),
        manifest=SimpleNamespace(schema=SimpleNamespace(columns=[SimpleNamespace(name=c) for c in columns])),
        result=SimpleNamespace(data_array=[list(r) for r in rows]),
        statement_id="st",
    )


def test_exec_sql_routes_to_remote_warehouse_and_never_spark():
    from genie_space_optimizer.optimization.benchmarking import _exec_sql

    home, remote = MagicMock(name="home"), MagicMock(name="remote")
    remote.statement_execution.execute_statement.return_value = _succeeded(("n",), (("1",),))
    _activate(remote)
    df = _exec_sql("SELECT 1 AS n", _NoSpark(), w=home, warehouse_id="home-wh")
    assert list(df["n"]) == ["1"]
    kw = remote.statement_execution.execute_statement.call_args.kwargs
    assert kw["warehouse_id"] == "remote-wh"
    assert not home.statement_execution.execute_statement.called


def test_exec_sql_routes_remote_even_without_a_warehouse_arg():
    from genie_space_optimizer.optimization.benchmarking import _exec_sql

    remote = MagicMock(name="remote")
    remote.statement_execution.execute_statement.return_value = _succeeded(("n",), (("1",),))
    _activate(remote)
    _exec_sql("SELECT 1 AS n", _NoSpark())
    assert remote.statement_execution.execute_statement.called


def test_exec_sql_warehouse_failure_does_not_fall_back_to_spark_when_remote():
    from genie_space_optimizer.optimization.benchmarking import _exec_sql

    remote = MagicMock(name="remote")
    remote.statement_execution.execute_statement.side_effect = RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
    _activate(remote)
    with pytest.raises(RuntimeError, match="TABLE_OR_VIEW_NOT_FOUND"):
        _exec_sql("SELECT * FROM cat.sch.t", _NoSpark(), w=MagicMock(), warehouse_id="home-wh")


def test_exec_sql_home_behaviour_unchanged_spark_fallback():
    from genie_space_optimizer.optimization.benchmarking import _exec_sql

    spark = MagicMock()
    spark.sql.return_value.toPandas.return_value = pd.DataFrame({"n": [1]})
    df = _exec_sql("SELECT 1 AS n", spark)
    assert list(df["n"]) == [1]


def test_validate_ground_truth_sql_uses_remote_warehouse():
    from genie_space_optimizer.optimization.benchmarks import validate_ground_truth_sql

    remote = MagicMock(name="remote")
    remote.statement_execution.execute_statement.return_value = _succeeded(("plan",), (("== Physical Plan ==",),))
    _activate(remote)
    ok, _reason = validate_ground_truth_sql(
        "SELECT a FROM cat.sch.t", _NoSpark(), "cat", "sch",
        w=MagicMock(name="home"), warehouse_id="home-wh", execute=True,
    )
    assert ok
    used = {c.kwargs["warehouse_id"] for c in remote.statement_execution.execute_statement.call_args_list}
    assert used == {"remote-wh"}


def test_spark_uc_metadata_readers_refuse_when_remote():
    from genie_space_optimizer.common import uc_metadata

    _activate()
    with pytest.raises(at.RemoteSparkUnavailable):
        uc_metadata.get_columns_for_tables(_NoSpark(), [("cat", "sch", "t")])
    with pytest.raises(at.RemoteSparkUnavailable):
        uc_metadata.get_tags(_NoSpark(), "cat", "sch")


def test_rest_uc_metadata_uses_remote_client():
    from genie_space_optimizer.common import uc_metadata

    home, remote = MagicMock(name="home"), MagicMock(name="remote")
    remote.tables.get.return_value = SimpleNamespace(columns=[], table_type=None, comment=None)
    _activate(remote)
    uc_metadata.get_columns_for_tables_rest(home, [("cat", "sch", "t")])
    assert remote.tables.get.called and not home.tables.get.called


def test_advisory_spark_probes_are_skipped_when_remote():
    from genie_space_optimizer.optimization import preflight

    _activate()
    assert preflight.check_dim_date_staleness(_NoSpark(), "cat", "sch")["status"] == "error"
    preflight._validate_write_access(_NoSpark(), [("cat", "sch", "t")])  # no raise, no Spark


def test_sp_alias_is_the_remote_sp_not_the_home_env(monkeypatch):
    from genie_space_optimizer.common.sp_permissions import _get_sp_client_id

    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "home-sp")
    remote = _activate()
    remote.config.client_id = None
    assert _get_sp_client_id(remote) == "remote-sp-id"


def test_llm_client_is_still_built_from_the_home_client(monkeypatch):
    from genie_space_optimizer.optimization import llm_client

    home = MagicMock(name="home")
    home.config.host = "https://home.example.com"
    home.config.token = "home-token"
    _activate()
    client = llm_client.get_openai_client(home)
    assert "home.example.com" in str(client.base_url)


def test_gso_state_helpers_are_not_rerouted():
    from genie_space_optimizer.common import warehouse

    home, remote = MagicMock(name="home"), MagicMock(name="remote")
    home.statement_execution.execute_statement.return_value = _succeeded(("n",), (("1",),))
    _activate(remote)
    warehouse.sql_warehouse_query(home, "home-wh", "SELECT 1 AS n")
    assert home.statement_execution.execute_statement.called
    assert not remote.statement_execution.execute_statement.called


# ── App-facing integration ───────────────────────────────────────────────


def test_job_launcher_sends_target(monkeypatch):
    from genie_space_optimizer.backend import job_launcher

    ws = MagicMock()
    ws.jobs.run_now.return_value = SimpleNamespace(run_id=42)
    job_launcher.submit_optimization(
        ws, job_id=7, run_id="r", space_id="s", domain="d", catalog="c", schema="sc",
        target='{"workspace_id": "ws_1"}',
    )
    assert ws.jobs.run_now.call_args.kwargs["job_parameters"]["target"] == '{"workspace_id": "ws_1"}'
    job_launcher.submit_optimization(ws, job_id=7, run_id="r2", space_id="s", domain="d", catalog="c", schema="sc")
    assert ws.jobs.run_now.call_args.kwargs["job_parameters"]["target"] == ""


def _patch_trigger(monkeypatch):
    import genie_space_optimizer.common.genie_client as gc
    from genie_space_optimizer.integration import trigger

    monkeypatch.setattr(trigger, "wh_ensure_optimization_tables", lambda *a, **k: None)
    monkeypatch.setattr(trigger, "sql_warehouse_query", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(trigger, "wh_reconcile_active_runs", lambda *a, **k: False)
    monkeypatch.setattr(trigger, "wh_create_run", MagicMock())
    monkeypatch.setattr(trigger, "sql_warehouse_execute", lambda *a, **k: None)
    seen: dict = {"fetch": [], "edit": 0, "manage": None, "read": None, "prefetch": None}

    def edit(*_a, **_k):
        seen["edit"] += 1
        return True

    def manage(client, space_id, aliases, *a, **k):
        seen["manage"] = (client, set(aliases))
        return True

    def fetch(client, _space_id):
        seen["fetch"].append(client)
        return {
            "title": "Remote Space",
            "_tables": ["rc.rs.t"],
            "_parsed_space": {"version": 2, "data_sources": {"tables": [{"identifier": "rc.rs.t"}], "metric_views": [], "functions": []}},
        }

    monkeypatch.setattr(gc, "user_can_edit_space", edit)
    monkeypatch.setattr(gc, "sp_can_manage_space", manage)
    monkeypatch.setattr(gc, "fetch_space_config", fetch)
    monkeypatch.setattr("genie_space_optimizer.common.sp_permissions.get_sp_principal_aliases", lambda c: {"me"})

    def read_check(client, schemas):
        seen["read"] = client
        return [], []

    monkeypatch.setattr("genie_space_optimizer.common.sp_permissions.missing_read_schemas", read_check)

    def prefetch(client, **kw):
        seen["prefetch"] = (client, kw)
        return {}

    monkeypatch.setattr("genie_space_optimizer.integration.uc_metadata.fetch_uc_metadata_obo", prefetch)
    submit = MagicMock(return_value=(987, 654))
    monkeypatch.setattr("genie_space_optimizer.backend.job_launcher.submit_optimization", submit)
    return trigger, seen, submit


def test_trigger_remote_uses_agent_ws_and_forwards_target(monkeypatch):
    from genie_space_optimizer.integration.config import IntegrationConfig

    trigger, seen, submit = _patch_trigger(monkeypatch)
    ws, sp_ws, agent_ws = MagicMock(name="ws"), MagicMock(name="sp"), MagicMock(name="agent")
    trigger.trigger_optimization(
        "space-1", ws, sp_ws,
        IntegrationConfig(catalog="cat", schema_name="sch", warehouse_id="home-wh", job_id=654),
        user_email="admin@example.com",
        agent_ws=agent_ws, target=dict(TARGET), agent_sp_aliases=["Remote-SP"],
    )
    assert seen["edit"] == 0  # no cross-workspace user identity
    assert seen["fetch"] and all(c is agent_ws for c in seen["fetch"])
    client, aliases = seen["manage"]
    assert client is agent_ws and {"remote-sp", "remote-sp-id", "me"} <= aliases
    assert seen["read"] is agent_ws
    pf_client, pf_kw = seen["prefetch"]
    assert pf_client is agent_ws and pf_kw["warehouse_id"] == "remote-wh"
    sent = submit.call_args.kwargs
    assert at.AgentTarget.from_json(sent["target"]) == at.AgentTarget.from_dict(TARGET)
    assert sent["warehouse_id"] == "home-wh"  # job's own (home) warehouse


def test_trigger_home_run_is_unchanged(monkeypatch):
    from genie_space_optimizer.integration.config import IntegrationConfig

    trigger, seen, submit = _patch_trigger(monkeypatch)
    ws, sp_ws = MagicMock(name="ws"), MagicMock(name="sp")
    trigger.trigger_optimization(
        "space-1", ws, sp_ws,
        IntegrationConfig(catalog="cat", schema_name="sch", warehouse_id="home-wh", job_id=654),
        user_email="u@example.com",
    )
    assert seen["edit"] == 1
    assert seen["manage"][0] is sp_ws
    assert submit.call_args.kwargs["target"] is None


def test_trigger_requires_both_agent_ws_and_target(monkeypatch):
    from genie_space_optimizer.integration.config import IntegrationConfig

    trigger, _seen, _submit = _patch_trigger(monkeypatch)
    with pytest.raises(ValueError, match="both agent_ws and target"):
        trigger.trigger_optimization(
            "space-1", MagicMock(), MagicMock(),
            IntegrationConfig(catalog="cat", schema_name="sch", warehouse_id="home-wh", job_id=654),
            user_email="u@example.com", agent_ws=MagicMock(),
        )


# ── Job definitions ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [REPO_ROOT / "databricks.yml", PKG_ROOT / "databricks.yml"])
def test_bundles_declare_target_in_params_and_every_task(path):
    doc = yaml.safe_load(path.read_text())
    for job in doc["resources"]["jobs"].values():
        assert any(p["name"] == "target" and p.get("default", None) == "" for p in job["parameters"])
        assert len(job["tasks"]) == 4
        for task in job["tasks"]:
            assert task["notebook_task"]["base_parameters"]["target"] == "{{job.parameters.target}}"


def test_job_notebooks_declare_and_apply_the_target_widget():
    jobs = PKG_ROOT / "src" / "genie_space_optimizer" / "jobs"
    for name in ("run_intake_and_snapshot", "run_benchmark_qc_and_repair", "run_optimize", "run_publish_and_audit"):
        src = (jobs / f"{name}.py").read_text()
        assert 'dbutils.widgets.text("target", "")' in src
        assert 'configure_agent_target_from_param(w, dbutils.widgets.get("target"))' in src
