---
sidebar_position: 4
description: "Where the app keeps its state: one Unity Catalog Delta table, how its location is chosen, and how writes are persisted."
---

# App State Storage

Genie Workbench keeps its own state in a single Unity Catalog Delta table:

```
<catalog>.<schema>.workbench_state
```

The state covers IQ scan results and history, starred and seen agents, Auto-Optimize history markers, join advice, app settings (such as the [release check](/docs/getting-started/deployment-guide#release-update-check) answer), GenieWatch caches, cross-workspace connections, data-transfer counters, and Create Agent sessions.

The app does not use Lakebase or any other database for this. Lakebase is not available in every region (GCP us-east1, for example), while Delta tables and a SQL warehouse are.

GSO run history (`genie_opt_*` tables) was always in Delta and is not part of this table. See [Auto-Optimize](/docs/features/auto-optimize#durable-state).

## Table layout

| Column | Type | Description |
|--------|------|-------------|
| `store` | STRING | Logical store, such as `scans`, `stars`, `app_settings` or `agent_sessions` |
| `k` | STRING | Key within the store |
| `v` | STRING | JSON payload |
| `updated_at` | TIMESTAMP | When the row was last written |

The table is created with `CLUSTER BY (store, k)`. The app creates the table itself at startup, and it owns it. The schema must already exist.

## Choosing the location

The location is a catalog and schema. There are three ways to set it, and the app uses the first one it finds:

1. **Setup dialog (default).** On first start no location is chosen. Workspace admins see a Setup dialog in the app and pick a catalog and schema. The dialog runs with the admin's own permissions. It can create the schema, then grants the app service principal `USE CATALOG` on the catalog and `USE SCHEMA, CREATE TABLE, SELECT, MODIFY` on the schema. The choice is saved to `/Users/<app-sp-client-id>/.genie-workbench/storage.json` in the workspace, so it survives restarts and is not tied to the admin who ran setup. Set `WORKBENCH_STORAGE_CONFIG_PATH` to use a different file.
2. **Saved setup file.** The file written by the Setup dialog above is read at startup.
3. **Pre-set at deploy time.** Set the location before the app starts:
   - Terminal installer: `GENIE_STATE_CATALOG` and `GENIE_STATE_SCHEMA` in `.env.deploy` (the installer asks for them in step 7). `grant_permissions.py` then creates the schema and grants the app service principal access.
   - Notebook installer: the `state_catalog` and `state_schema` widgets. Leave `state_catalog` blank to choose the location later in the Setup dialog.

   Both surface in the app as the environment variables `WORKBENCH_STATE_CATALOG` and `WORKBENCH_STATE_SCHEMA`.

If you install without a location, the Setup dialog stays up for admins (it can be dismissed for the browser session) until someone completes it.

## Until storage is configured

With no location, the app runs in memory only. Scans, stars, settings and sessions work, but they are lost when the app restarts. An app setting such as the release-check answer is then not saved for the whole app; it applies only in the browser that set it.

If a location is configured but the table cannot be created or read (for example, a missing grant), the app keeps running in memory and retries on later requests, at most every 30 seconds. Its log shows `Delta state store init failed`. Settings writes fail with an error in this state rather than being kept only in memory.

## Who runs the SQL

All state reads and writes go through the app's SQL warehouse as the **home** service principal. They never follow a cross-workspace target, and they do not depend on the calling user's permissions. The only user-permission SQL is the one-time Setup step described above.

## How writes are persisted

- **Reads come from memory.** The app loads the state into memory at startup, in the background. Request handlers never wait on the warehouse to read state.
- **Changed keys are flushed about every 15 seconds.** Only keys that changed are written, with `MERGE` for values that exist and `DELETE` for values that were removed. A final flush runs at shutdown.
- **Some writes are synchronous and fail closed.** App settings and "remove run from history" are written to Delta before the request returns. If storage is unavailable, the request fails instead of silently keeping the change in memory.
- **Create Agent sessions are read lazily.** They are not loaded at startup. A session missing from memory is read from Delta with a point lookup when it is needed.
- **Single app instance.** The app runs as one container with one in-memory copy of the state. If it were ever scaled out, each replica would hold its own copy and the last write would win.

## Permissions for the service principal

The app service principal needs these grants on the state schema. The Setup dialog and `grant_permissions.py` apply them for you:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app-sp-client-id>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA <catalog>.<schema> TO `<app-sp-client-id>`;
```

## Upgrades and teardown

- Installs that used Lakebase do not migrate their old data. Read the [upgrade note](/docs/getting-started/upgrading#upgrading-from-a-lakebase-install) before upgrading.
- `./scripts/deploy.sh --destroy` does not drop `workbench_state`. Drop the table and its schema manually for a full teardown. See the [Deployment Guide](/docs/getting-started/deployment-guide).

## Related Documentation

- [Operations Guide](/docs/platform/operations#app-state-delta) — monitoring, log messages and troubleshooting for app state
- [Environment Variables](/docs/reference/environment-variables#app-state-delta) — the variables that set the location
- [Cross-Workspace](/docs/platform/cross-workspace) — remote workspace connections, whose secrets are kept in a home secret scope
