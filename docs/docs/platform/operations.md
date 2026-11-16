---
sidebar_position: 2
description: "App state storage, MLflow, monitoring, and GSO job management for a deployed instance."
---

# Operations Guide

This guide covers day-to-day operations for a deployed Genie Workbench instance: app state storage, MLflow configuration, monitoring, and GSO job management.

## App State (Delta)

The app keeps its state in one Unity Catalog Delta table, `<catalog>.<schema>.workbench_state`. There is no database to run or refresh credentials for. See [App State Storage](/docs/platform/storage) for the table layout, how the location is chosen, and the write behaviour.

| Data | Store (`store` column) |
|------|------------------------|
| IQ scan results and history | `scans`, `scan_history` |
| Starred and seen agents | `stars`, `seen` |
| Auto-Optimize run markers and removed-run tombstones | `optimization_runs`, `hidden_runs` |
| Join advice | `join_advice` |
| App settings (such as the release-check answer) | `app_settings` |
| GenieWatch caches | `watch_*` |
| Cross-workspace connections and transfer counters | `workspace_connections`, `workspace_transfer` |
| Create Agent sessions | `agent_sessions` |

All reads come from memory. Changed keys are flushed about every 15 seconds, and once more at shutdown. App settings and "remove run from history" are written before the request returns and fail if storage is unavailable.

### Checking the state location

Open **Admin** as a workspace admin. If the Setup dialog appears, no location is configured and the app is running in memory only. Complete the dialog to choose one. The saved choice is at `/Users/<app-sp-client-id>/.genie-workbench/storage.json`.

### Troubleshooting App State

| Symptom | Cause | Fix |
|---------|-------|-----|
| Setup dialog keeps appearing for admins | No location is saved, or the saved file is incomplete (`Saved storage setup at … is incomplete`) | Complete the Setup dialog again. If the log shows `Failed to save storage setup`, check that the app SP can write to its workspace home folder, or set `WORKBENCH_STORAGE_CONFIG_PATH` |
| Scans, stars or sessions disappear on restart | No location configured, so the app is in memory only | Complete the Setup dialog, or set the location at install (see [Storage](/docs/platform/storage#choosing-the-location)) |
| `Delta state store init failed` in logs | The state table or schema is missing, or a grant is missing | Check that the SP has `USE CATALOG`, `USE SCHEMA`, `CREATE TABLE`, `SELECT`, `MODIFY` on the schema. Re-run the Setup step as an admin, or rerun the installer. The app retries on later requests |
| Scan results not persisting | Write failed, usually a warehouse or grant problem | Check logs for `Delta flush failed for '<store>'; will retry` |
| "Remove from history" or a settings change fails | Storage is configured but unavailable, so the write is refused rather than kept in memory | Check the warehouse is running and the SP's grants, then retry |

## MLflow

### Experiment Tracking

LLM calls in the create agent and optimization pipeline are traced via MLflow. Tracing is **optional** — controlled by the `MLFLOW_EXPERIMENT_ID` environment variable in `app.yaml`.

At startup, the app validates that the experiment ID exists in the workspace. If it doesn't, tracing is silently disabled (the variable is cleared).

Tracing is the **only** MLflow dependency. Auto-Optimize does not use MLflow for
dataset persistence, run tracking, model registration, or evaluation — the
benchmark corpus is written directly to Delta and evaluation uses Genie's native
Eval-Run API. MLflow Prompt Registry is not required, and there is no
`MLFLOW_REGISTRY_URI` setting.

### Configuration

```yaml
# In app.yaml
- name: MLFLOW_TRACKING_URI
  value: "databricks"
- name: MLFLOW_EXPERIMENT_ID
  value: "<your-experiment-id>"
```

The experiment ID is workspace-specific. The installer can create one during setup, or you can create one manually and update `app.yaml`.

## Monitoring

### App Logs

```bash
databricks apps logs <app-name> --profile <profile>
```

### App Status

```bash
databricks apps get <app-name> --profile <profile>
```

### Verify Workspace Files

```bash
databricks workspace list /Workspace/Users/<email>/<app-name>/backend --profile <profile>
```

### Key Log Patterns

| Log Pattern | Meaning |
|-------------|---------|
| `OBO: using user token for /api/...` | Request authenticated via user's OBO token |
| `OBO: no x-forwarded-access-token, using SP` | No user token — using SP (expected for health checks) |
| `OBO token lacks genie scope, retrying with service principal` | Genie API scope fallback triggered |
| `user token lacks the ai-gateway scope; retrying the gateway call as the service principal` | Gateway scope fallback: the user hasn't consented to the `ai-gateway` scope, so the call runs as the SP |
| `Gateway model unavailable/entitlement 404 for <model>` | The model doesn't exist on Unity AI Gateway, or the caller isn't entitled to it |
| `Delta state store ready at <catalog>.<schema>.workbench_state` | App state loaded from Delta at startup |
| `Workbench storage not configured yet (Setup wizard pending)` | No state location yet; running in memory until the Setup dialog is completed |
| `Delta state store init failed: … Using memory until it recovers.` | The state table could not be created or loaded; retried on later requests |
| `Delta flush failed for '<store>'; will retry` | A flush of changed keys failed; the keys stay pending for the next flush |
| `Final Delta flush failed` | The shutdown flush failed; writes since the last flush may be lost |
| `Delta point read failed for <store>/<key>` | A Create Agent session could not be read from Delta; treated as absent |
| `Saved storage setup to <path>` | The Setup dialog saved the state location |

## LLM Usage and Cost Attribution

On the gateway route (the default), every LLM call carries a `Databricks-Ai-Gateway-Request-Tags` header, which lands in the `request_tags` map of `system.ai_gateway.usage`. Every call is tagged `application: genie-workbench` plus a `component` (`create-agent`, `plan-builder`, `gso-optimize`, `mv-suggest`, `leakage-embed`). Optimizer calls add `run_id`, and Create Agent sessions started from an existing space add `space_id`. See [LLM calls](/docs/getting-started/architecture-overview#llm-calls) for which caller sends which tag.

Usage by feature and identity over the last day:

```sql
SELECT request_tags['component'] AS component,
       requester_type,
       count(*) AS requests,
       sum(total_tokens) AS tokens
FROM system.ai_gateway.usage
WHERE request_tags['application'] = 'genie-workbench'
  AND event_time > current_timestamp() - INTERVAL 1 DAY
GROUP BY ALL
ORDER BY tokens DESC
```

For one optimization run, filter on `request_tags['run_id'] = '<run_id>'`. `requester_type` separates calls made as a user from calls made as the app's service principal. Run the query as an identity that can read `system.ai_gateway`. Rows can take about 20 minutes to appear. Calls on the classic route are not recorded in this table at all.

## GSO Job Management

### Job Creation

The optimization job (`<app-name>-gso-optimization-job`) is created automatically by both install paths:

- **Notebook path (recommended):** `notebooks/install.py` creates or updates the job through the SDK/Jobs API (`jobs/reset` upsert semantics via `scripts/deploy_lib/gso_job.py`). No Terraform state is involved.
- **Local terminal path:** `deploy.sh` uses DABs (`databricks bundle deploy -t app`), with Terraform state scoped to the deployer.

### Job Reuse

If a job with matching settings already exists, it is reused rather than duplicated. To force recreation:

1. Delete the job in the Databricks UI
2. Rerun `notebooks/install.py`, or `./scripts/deploy.sh --update` for the local terminal path

### `ensure_job_run_as` Self-Healing

At app startup, `_ensure_gso_job_run_as()` checks that the optimization job's `run_as` matches the current app SP. If they don't match (e.g., the app was redeployed with a different SP), the job is automatically updated. This avoids manual reconfiguration when the app identity changes. If the update fails, the app logs `Could not verify GSO job run_as` and keeps running.

The local terminal path never sets `run_as`; startup sets it. The notebook installer sets `run_as` to the app SP when the installing user holds the `servicePrincipal.user` role on it, and otherwise creates the job without `run_as` and leaves it to startup.

If `GSO_JOB_ID` is empty, the app boots, and `/trigger` returns 503 `Auto-Optimize is not configured. Set GSO_CATALOG and GSO_JOB_ID.` (`auto_optimize.py`, `_is_configured`).

### Bundle Management (local terminal path only)

On the local terminal path, the GSO job is managed by Databricks Asset Bundles (DABs):

```bash
# Deploy/update the job (done automatically by deploy.sh)
databricks bundle deploy -t app --profile <profile>
```

**Important:** Do NOT run `databricks bundle deploy -t dev` for production deployments — it creates `[dev username]` prefixed orphan jobs with separate Terraform state.

The `app` target uses `mode: development` for per-deployer Terraform state with `presets.name_prefix: ""` for clean job names.

The notebook installer does not use DABs at all — it manages the job through the Jobs API, so there is no bundle or Terraform state to maintain. Do not mix the two paths for the same app instance.

### Post-Deploy: Genie Agent Access

After deploying, the app's SP needs access to Genie Agents for API fallback and optimization:

1. Both installers grant SP access to your existing visible Genie Agents
2. For agents created after install, share them with the SP (`CAN_MANAGE`)
3. Grant SP `SELECT` on referenced schemas:

```sql
GRANT SELECT ON SCHEMA <catalog>.<schema> TO `<service-principal-name>`;
```

See [Authentication & Permissions](/docs/platform/authentication) for the full permission model.

## Related Documentation

- [Deployment Guide](/docs/getting-started/deployment-guide) — initial setup and deploy commands
- [Authentication & Permissions](/docs/platform/authentication) — SP permissions
- [Auto-Optimize](/docs/features/auto-optimize) — the pipeline managed by the GSO job
- [Troubleshooting](/docs/reference/troubleshooting) — common issues
