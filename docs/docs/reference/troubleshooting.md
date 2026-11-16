---
sidebar_position: 3
description: "Common issues, causes, and fixes."
---

# Troubleshooting

## Common Issues

"Re-run your install path" below means rerunning `notebooks/install.py` from the
top (notebook path, recommended) or `./scripts/deploy.sh --update` (local
terminal path). Use whichever path you originally installed with — don't mix
them for the same app instance.

| Symptom | Cause | Fix |
|---------|-------|-----|
| App shows blank page | `frontend/dist/` missing (gitignored, not synced) | Re-run your install path |
| `Could not import module "backend.main"` | Source files missing on workspace | Re-run your install path (full sync) |
| `No dependencies file found` | `pyproject.toml` / `uv.lock` not on workspace | Re-run your install path |
| `Catalog 'X' is not accessible` | Wrong catalog or missing permissions | Verify the `catalog` widget value (notebook) or `GENIE_CATALOG` (terminal); `databricks catalogs list --profile <profile>` |
| `Invalid SQL warehouse resource` | Warehouse doesn't exist or no CAN_USE | `databricks warehouses list --profile <profile>` |
| `Maximum number of apps` | Workspace hit the 300-app limit | Delete unused apps |
| Unresolved `__GSO_*__` placeholders | The installer couldn't patch `app.yaml` | Ensure the catalog is set; check installer output for warnings |
| GSO job creation fails during deploy | Notebook path: Jobs API error or wheel build failure. Terminal path: bundle deploy failed (CLI version, auth, build) | Notebook: check the install cell output. Terminal: check `databricks bundle deploy -t app` output; ensure CLI >= 0.297.2 and `pip install build` |
| Notebook upload fails (`RESOURCE_DOES_NOT_EXIST`) | Upload path not writable by deployer | Check workspace-level permissions on the upload path |

## Permission Errors

| Symptom | Cause | Fix |
|---------|-------|-----|
| "You need CAN_EDIT or CAN_MANAGE permission" on optimize trigger | User lacks permission on the Genie Agent | Share the agent with the user (CAN_EDIT or CAN_MANAGE) |
| "The service principal does not have CAN_MANAGE" | SP not shared on the Genie Agent | Share the agent with the app's SP (CAN_MANAGE) |
| "The app service principal cannot read N schema(s) this space uses" (403 on optimize trigger) | SP lacks `USE CATALOG`, `USE SCHEMA` or `SELECT` on a schema the agent references | Run the GRANTs the Auto-Optimize permission check shows for that schema, then retry |
| "OBO token lacks genie scope, retrying with service principal" (in logs) | User token missing `dashboards.genie` scope | This is handled automatically via SP fallback — no action needed unless SP also fails |
| Optimization job fails with catalog/schema access errors | SP lacks UC permissions on referenced data | Grant `SELECT` on referenced schemas to the SP |
| "Permission denied" on scan | User lacks access to the Genie Agent | Share the agent with the user |

## App State (Delta) Issues

See [App State Storage](/docs/platform/storage) and [Operations](/docs/platform/operations#troubleshooting-app-state) for the full picture.

| Symptom | Cause | Fix |
|---------|-------|-----|
| Setup dialog appears for admins | No state location is chosen yet | Complete the Setup dialog as a workspace admin, or set `state_catalog` / `GENIE_STATE_CATALOG` at install |
| Scans, stars or agent sessions lost on restart | No state location configured, so state is in memory only | Complete the Setup dialog, or set the location at install |
| `Delta state store init failed` in logs | The state table or schema is missing, or the app SP lacks a grant | Grant the app SP `USE CATALOG` on the catalog and `USE SCHEMA, CREATE TABLE, SELECT, MODIFY` on the schema, then wait for the next request (it retries) |
| Scan results not persisting | Delta flush failed | Check logs for `Delta flush failed for '<store>'; will retry` and the warehouse status |
| "Remove from history" or a settings change fails | Storage is configured but unavailable | Check the warehouse is running and the SP's grants, then retry |

## Cross-Workspace Issues

See [Cross-Workspace](/docs/platform/cross-workspace) for registration and the required grants.

| Symptom | Cause | Fix |
|---------|-------|-----|
| `403` "Only workspace admins can work in other workspaces." | The caller is not a home-workspace admin | Use an admin account, or switch back to the home workspace |
| `404` "The selected workspace is no longer registered." | The connection was removed while it was selected | Switch back to the home workspace |
| `502` "Could not connect to the selected workspace" | Authentication or network failure to the remote host | In **Admin → Workspaces**, use **Test**, then check the host and the SP's client ID and secret |
| Connection shows **No system tables** | The remote SP cannot read system tables | Apply the system-table grants in the remote workspace |
| Test says "no SQL warehouse visible" | The remote SP cannot see a warehouse | Grant `CAN USE` on a warehouse in the remote workspace, or set one on the connection |
| `403` "Cert validation failed. Workspace comparison, snp system trusted…" | An account-level cross-workspace network policy (on GCP in particular) rejects the call | Requires account-level onboarding by Databricks. The app cannot work around it |
| `400` "Metric view and join advisors are available only for agents in this app's home workspace." | The advisor routes are home-only | Use the advisors from the agent's home workspace |
| `400` on Auto-Optimize for a remote agent | The connection is `app_sp`; remote optimization needs `custom_sp` | Register the workspace as `custom_sp` and give the remote SP `CAN MANAGE` on the agent |

## GSO / Auto-Optimize Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| "GSO not configured" in health check | `GSO_JOB_ID` or `GSO_CATALOG` not set | Re-run your install path (patches `app.yaml` with the resolved job ID and catalog) |
| Optimization job never starts | Job doesn't exist or SP can't run it | Check job exists in workspace; verify SP has CAN_MANAGE on job |
| Job stuck in QUEUED | No available cluster or warehouse | Check cluster policies and warehouse availability |
| Baseline evaluation fails | Benchmark questions reference inaccessible tables | Grant SP `SELECT` on all referenced schemas |
| Run ends `SKIPPED` with `INSUFFICIENT_VALID_BENCHMARKS` | Fewer than 15 valid benchmarks remained after QC and the bounded top-up budget | Review the top-up attempt telemetry and rejected findings, then add or repair benchmark questions before rerunning |
| Run ends `FAILED` with `EVAL_INVALID` | Native Eval-Run API returned an unusable result after retries | Check the Eval-Run status in the run page; verify SP access to referenced tables |
| Patches generated but accuracy doesn't improve | Optimization strategy exhausted | Run may reach `STALLED` status — review suggestions for manual improvements |
| `__GSO_*__` values in running app | The installer didn't patch `app.yaml` before deploy | Verify the catalog value and re-run your install path |

## LLM / Unity AI Gateway Issues

LLM calls use Unity AI Gateway unless the app was installed with `llm_route=classic` (`GENIE_LLM_ROUTE=classic`). See [LLM calls](/docs/getting-started/architecture-overview#llm-calls) and [Model access for LLM calls](/docs/platform/authentication#model-access-for-llm-calls).

| Symptom | Cause | Fix |
|---------|-------|-----|
| Install stops with `Unity AI Gateway check failed for model '<model>'` | The workspace's region has no Unity AI Gateway, the model has no `system.ai` name, or the installing user can't use it | Pick a model available on the gateway, or reinstall with `llm_route=classic` (`GENIE_LLM_ROUTE=classic` for `deploy.sh`) |
| `Model unavailable or access not granted.` in Create Agent or the plan builder | Gateway `404`: the model doesn't exist on the gateway, or the signed-in user isn't entitled to it | Check `LLM_MODEL` maps to an existing `system.ai.<name>`; grant the user access to the model |
| An optimization run's LLM steps fail with `Model unavailable or access not granted.` (job task output) | The same `404`, for the app service principal (the job's `run_as`). The install check ran as you, not as the SP | Grant the app's service principal access to the model |
| A custom serving endpoint stops working after the upgrade | Only `databricks-*` foundation models have a `system.ai` name on the gateway | Reinstall with `llm_route=classic` |
| `user token lacks the ai-gateway scope; retrying the gateway call as the service principal` (in logs) | The user hasn't accepted the `ai-gateway` scope yet, or the request carried a token that never went through the app's consent screen (such as a CLI token) | Handled automatically. Until the user consents, their calls are attributed to the SP in `system.ai_gateway.usage`. Opening the app in a browser shows the consent screen |
| No `genie-workbench` rows in `system.ai_gateway.usage` | Ingestion lag (allow about 20 minutes), or the app is on the classic route | Wait and requery; check `GENIE_LLM_ROUTE` in the deployed `app.yaml` |

## Debug Commands

```bash
# View app logs
databricks apps logs <app-name> --profile <profile>

# Check app status
databricks apps get <app-name> --profile <profile>

# List workspace files to verify sync
databricks workspace list /Workspace/Users/<email>/<app-name>/backend --profile <profile>

# Check GSO job status
databricks jobs get <job-id> --profile <profile>

# List GSO job runs
databricks jobs list-runs --job-id <job-id> --profile <profile>

# Check SP identity
databricks apps get <app-name> --profile <profile> | grep service_principal
```

## MLflow Tracing

:::note
`MLFLOW_EXPERIMENT_ID` is workspace-specific. The app validates it at startup and silently disables tracing if the experiment doesn't exist. To enable tracing, create an MLflow experiment and update the value in `app.yaml` before deploying.
:::

## Related Documentation

- [Deployment Guide](/docs/getting-started/deployment-guide) — deploy commands and configuration
- [Operations Guide](/docs/platform/operations) — monitoring and management
- [Authentication & Permissions](/docs/platform/authentication) — permission model
- [Environment Variables](/docs/reference/environment-variables) — full variable reference
