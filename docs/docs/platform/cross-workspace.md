---
sidebar_position: 5
description: "Score, monitor, create and optimize Genie Agents in other workspaces of the same Databricks account from one home deployment."
---

# Cross-Workspace

Genie Workbench is deployed in one **home** workspace but can work with Genie Agents in other (**remote**) workspaces of the same Databricks account. From the home deployment you can list and score agents, create agents with Create Agent, monitor them with GenieWatch, and run Auto-Optimize against them.

Some things always stay in the home workspace. See [Home-only features](#home-only-features).

## How it works

Your signed-in user identity and the app's own service principal (SP) act in the home workspace. A remote workspace is reached with **service-principal OAuth (M2M) credentials**, so each remote workspace is registered as a *connection* in **Admin → Workspaces**.

To work in a remote workspace, an admin selects it in the **workspace switcher** in the header. The browser then sends an `X-Target-Workspace: <id>` header with each request. The app resolves that id to a cached client for the remote workspace and uses it for the request.

```mermaid
flowchart LR
    browser["Browser<br/>(X-Target-Workspace header)"] --> mw["OBO + Target middleware"]
    mw -->|"target set"| remote["Remote SP client<br/>(OAuth M2M · remote host)"]
    mw -->|"no target"| home["Home OBO / SP client"]
    remote --> genie["Genie API · UC · SQL warehouse · system tables"]
    home --> homeonly["LLM serving · app state · GSO job · identity"]
    classDef box font-size:18px;
    class browser,mw,remote,home,genie,homeonly box;
```

Remote calls run as the remote service principal. There is no per-user identity in the remote workspace, so **targeting another workspace is limited to home-workspace admins**. Non-admins get `403`.

An unknown or broken target is an error, never a silent fallback to the home workspace:

| Response | Meaning |
|----------|---------|
| `403` | The caller is not a workspace admin |
| `404` | The selected workspace is no longer registered. Switch back to home |
| `502` | The app could not connect to the selected workspace |

The switcher's selection is kept in the browser and applied before the first request after a reload.

## Registering a remote workspace

Connections are managed in **Admin → Workspaces** by workspace admins. Each connection is one of two modes:

| Mode | Credentials | Use when |
|------|-------------|----------|
| `app_sp` | The app's own service principal. An admin adds it to the remote workspace. No secret is stored. | The app's SP can be added to the remote workspace |
| `custom_sp` | A dedicated service principal registered in the remote workspace, with its client ID and OAuth secret. The secret is stored in a Databricks secret scope in the **home** workspace (`genie-workbench-workspaces` by default; set `GENIE_WORKSPACES_SECRET_SCOPE` at deploy to change it). The state table keeps only the scope and key reference. | You want a dedicated identity, or you want Auto-Optimize to run in the remote workspace |

For either mode you can enter a remote SQL warehouse ID. If you leave it blank, the app auto-detects a warehouse the remote SP can use.

To register a `custom_sp` connection:

1. In the remote workspace, create a service principal and generate an OAuth secret for it.
2. In Genie Workbench, open **Admin → Workspaces → Add workspace**. Enter a name, the remote host (`https://…`), and the SP client ID and secret.
3. Save. The credentials are validated against the remote workspace before anything is stored. A host that already has a connection is rejected.
4. Select the workspace in the header switcher.

Connection status:

- **Active**: authenticated and system tables are readable, so monitoring works.
- **No system tables**: authenticated, but the SP cannot read system tables. Scoring and deployment work, and monitoring panels stay empty.
- **Error**: authentication failed. Use **Test** to probe the connection again.

Editing a connection with a new secret rotates the stored secret. Deleting a connection removes its secret.

## What works remotely

- **Listing and IQ scans** of agents the remote SP can see.
- **Create Agent.** Agents are created in the remote workspace. LLM calls still run in the home workspace.
- **GenieWatch monitoring.** System tables are queried in the remote workspace, on its warehouse. See [System-table grants](#system-table-grants-for-monitoring).
- **Auto-Optimize.** The optimization job runs in the home workspace, so the LLM, GSO run state and MLflow stay at home. Benchmark ground-truth SQL, profiling, Unity Catalog metadata and Genie updates run in the remote workspace on its warehouse. This works across separate metastores, for example workspaces in different regions.

### Auto-Optimize requirements

- The connection must be `custom_sp`. For an `app_sp` connection the API returns `400`, because the home job cannot sign in to another workspace without a dedicated secret.
- The remote SP needs **CAN MANAGE** on the agent and read access to the tables it references.
- The **Metric view** and **Join advisor** routes are home-only. For a remote agent they return `400`, and the Model tab's advisors are hidden. Score, Optimize and History work.

## Home-only features

These always run in the home workspace, whichever workspace is selected:

- The **Model** tab's metric-view and join advisors.
- **Version Control.**
- **LLM serving** (model calls and the model catalog).
- **App state**: the [state table](/docs/platform/storage).
- **Storage setup** and **connection management** (`/api/workspaces`, `/api/setup`).

## Remote prerequisites per capability

Grant the remote SP what each capability needs **in the remote workspace**:

| Capability | Remote SP needs |
|------------|-----------------|
| **Score** (IQ scan) | `CAN VIEW` or `CAN EDIT` on the agents to list and score. `SELECT` on the upstream UC tables for metadata enrichment. |
| **Create** (Create Agent) | Permission to create Genie Agents, and a target directory there |
| **Monitor** (GenieWatch) | [System-table grants](#system-table-grants-for-monitoring), and `CAN USE` on a SQL warehouse in the remote workspace |
| **Optimize** (Auto-Optimize) | `CAN MANAGE` on the agent, `SELECT` (with `USE CATALOG` and `USE SCHEMA`) on its tables, and a `custom_sp` connection |

:::note Agents not showing up?
The **Agents** list only shows agents the remote SP can see. If your agent is owned by you rather than the SP, share it with the SP (`CAN VIEW` or `CAN EDIT`) in the remote workspace, then refresh.
:::

### System-table grants for monitoring

Run these in the **remote** workspace as a metastore admin there, substituting the remote SP's client ID. This is the same grant list the installer applies to the home workspace (`WATCH_SYSTEM_GRANTS` in `scripts/deploy_lib/uc.py`):

```sql
GRANT USE CATALOG ON CATALOG system                          TO `<remote-sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA  system.query                    TO `<remote-sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA  system.billing                  TO `<remote-sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA  system.access                   TO `<remote-sp-client-id>`;
GRANT SELECT      ON TABLE   system.query.history            TO `<remote-sp-client-id>`;
GRANT SELECT      ON TABLE   system.billing.usage            TO `<remote-sp-client-id>`;
GRANT SELECT      ON TABLE   system.billing.list_prices      TO `<remote-sp-client-id>`;
GRANT SELECT      ON TABLE   system.access.audit             TO `<remote-sp-client-id>`;
GRANT SELECT      ON TABLE   system.access.table_lineage     TO `<remote-sp-client-id>`;
GRANT SELECT      ON TABLE   system.access.column_lineage    TO `<remote-sp-client-id>`;
```

:::note Empty cost with no error
Monitoring reports SQL-warehouse compute attributed to **Genie query activity**. If an agent has been created or scanned but not used to run queries, the Cost tab is legitimately zero. Query counts and users appear within minutes of real usage. The cost estimate (`system.billing.usage`) can lag by a few hours because of the billing pipeline.
:::

## Data transfer observability

Cross-workspace reads can cross regions or clouds, so the app meters how much data it pulls **in** from each remote workspace. **Admin → Data Transfer** shows the volume per workspace, a daily trend, and an optional cost estimate.

**How it is measured.** No single Databricks metric equals app egress, so the app counts response-payload bytes at its own outbound call sites and attributes them to the selected workspace (`backend/services/transfer_meter.py`):

| Category | Source |
|----------|--------|
| **Genie** | Genie Agent config fetch and list (deploy and score) |
| **SQL** | Ad-hoc SQL and statement-execution results (score and scan) |
| **Monitoring** | System-table reads for the Cost, Usage, Feedback and Resources tabs |

Only **remote** pulls are counted. Home traffic has no egress cost and is not counted. This is an app-observed proxy, not the cloud provider's billed figure.

**Cost estimate.** Rates vary by cloud and discount, so the tab does not assume one. Enter your own `$/GB` rate. The browser keeps it, and the tab multiplies it by transferred GB (1 GB = 10⁹ bytes).

**Persistence.** Counters accumulate in memory by workspace, UTC day and category. They are flushed to the app state table every 30 seconds and at shutdown, so totals survive restarts. The tab is admin-only. Non-admins get an empty summary and the tab is hidden.

## Region caveat

Some accounts enforce a cross-workspace network policy. On GCP in particular, calls from one workspace to another can be rejected with a `403` such as "Cert validation failed. Workspace comparison, snp system trusted…". Databricks must onboard the accounts at the account level to lift this. The app cannot work around it, so a connection to such a workspace will show an error. See [Troubleshooting](/docs/reference/troubleshooting#cross-workspace-issues).

## Related Documentation

- [Authentication & Permissions](/docs/platform/authentication) — OBO and service principal identities
- [App State Storage](/docs/platform/storage) — where connections and transfer counters are stored
- [Operations Guide](/docs/platform/operations) — monitoring and job management
