#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy-config.sh — shared configuration for deploy.sh
#
# Sourced (not executed) by deploy scripts. Reads deployment settings from
# environment variables with sensible defaults and validates required values.
#
# Environment variables (set these before running deploy.sh):
#
#   GENIE_WAREHOUSE_ID       (required)  SQL Warehouse ID for query execution
#   GENIE_CATALOG            (required)  Unity Catalog name (must have CREATE SCHEMA permission)
#   GENIE_APP_NAME           (optional)  Databricks App name          [default: genie-workbench]
#   GENIE_DEPLOY_PROFILE     (optional)  Databricks CLI profile       [default: DEFAULT]
#   GENIE_LLM_MODEL          (optional)  Default LLM serving endpoint [default: databricks-claude-sonnet-4-6]
#   GENIE_LLM_ROUTE          (optional)  gateway | classic            [default: gateway]
#   GENIE_BYOK_ENABLED       (optional)  true | false (gateway only)  [default: false]
#   GENIE_SELECTABLE_MODELS  (optional)  Comma-separated endpoints for the model picker [default: curated list]
#   GENIE_STATE_CATALOG      (optional)  Catalog for the app's Delta state tables [default: chosen in the app's Setup dialog]
#   GENIE_STATE_SCHEMA       (optional)  Schema for the app's Delta state tables  [default: genie_workbench_state]
#   GENIE_WORKSPACES_SECRET_SCOPE (optional) Secret scope for cross-workspace SP secrets [default: genie-workbench-workspaces]
#   GENIE_MLFLOW_EXPERIMENT_ID (optional) MLflow experiment ID for agent tracing [default: disabled]
#   GENIE_UPDATE_CHECK       (optional)  true | false | empty (ask users on the Admin tab) [default: empty]
#
# After sourcing, the following variables are available:
#   APP_NAME, CATALOG, GSO_SCHEMA, WAREHOUSE_ID, PROFILE, LLM_MODEL, LLM_ROUTE,
#   BYOK_ENABLED, MLFLOW_EXPERIMENT_ID, UPDATE_CHECK, STATE_CATALOG, STATE_SCHEMA,
#   WORKSPACES_SECRET_SCOPE
# ---------------------------------------------------------------------------

# ── Load .env.deploy if present (in project root) ─────────────────────────
_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_DEPLOY_ENV="$_PROJECT_DIR/.env.deploy"
if [ -f "$_DEPLOY_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$_DEPLOY_ENV"
    set +a
fi

# ── Resolve config from env vars ─────────────────────────────────────────
APP_NAME="${GENIE_APP_NAME:-genie-workbench}"
CATALOG="${GENIE_CATALOG:-}"
GSO_SCHEMA="genie_space_optimizer"  # Fixed default — matches GSO convention
WAREHOUSE_ID="${GENIE_WAREHOUSE_ID:-}"
PROFILE="${GENIE_DEPLOY_PROFILE:-DEFAULT}"
LLM_MODEL="${GENIE_LLM_MODEL:-databricks-claude-sonnet-4-6}"
LLM_ROUTE="$(echo "${GENIE_LLM_ROUTE:-gateway}" | tr '[:upper:]' '[:lower:]')"
BYOK_ENABLED="$(echo "${GENIE_BYOK_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
SELECTABLE_MODELS="${GENIE_SELECTABLE_MODELS:-}"
STATE_CATALOG="${GENIE_STATE_CATALOG:-}"
STATE_SCHEMA="${GENIE_STATE_SCHEMA:-genie_workbench_state}"
if [ -z "$STATE_CATALOG" ]; then STATE_SCHEMA=""; fi
WORKSPACES_SECRET_SCOPE="${GENIE_WORKSPACES_SECRET_SCOPE:-genie-workbench-workspaces}"
if [ -n "${GENIE_LAKEBASE_INSTANCE:-}" ]; then
    echo "WARNING: GENIE_LAKEBASE_INSTANCE is ignored — this build stores app state in Delta tables, not Lakebase." >&2
fi
MLFLOW_EXPERIMENT_ID="${GENIE_MLFLOW_EXPERIMENT_ID:-}"
UPDATE_CHECK_RAW="${GENIE_UPDATE_CHECK:-}"
case "$(echo "$UPDATE_CHECK_RAW" | tr '[:upper:]' '[:lower:]')" in
    true|yes|y|1)  UPDATE_CHECK="true" ;;
    false|no|n|0)  UPDATE_CHECK="false" ;;
    *)             UPDATE_CHECK="" ;;
esac

# ── Validate required values ─────────────────────────────────────────────
if [ -z "$WAREHOUSE_ID" ]; then
    echo "ERROR: GENIE_WAREHOUSE_ID is required but not set." >&2
    echo "" >&2
    echo "Set it as an environment variable:" >&2
    echo "  export GENIE_WAREHOUSE_ID=<your-sql-warehouse-id>" >&2
    echo "" >&2
    echo "Or create a .env.deploy file in the project root:" >&2
    echo "  echo 'GENIE_WAREHOUSE_ID=<your-sql-warehouse-id>' >> .env.deploy" >&2
    exit 1
fi

if [ -z "$CATALOG" ]; then
    echo "ERROR: GENIE_CATALOG is required but not set." >&2
    echo "" >&2
    echo "Set it as an environment variable:" >&2
    echo "  export GENIE_CATALOG=<your-catalog>" >&2
    exit 1
fi

if [ "$LLM_ROUTE" != "gateway" ] && [ "$LLM_ROUTE" != "classic" ]; then
    echo "ERROR: GENIE_LLM_ROUTE must be 'gateway' or 'classic' (got '$GENIE_LLM_ROUTE')." >&2
    exit 1
fi

if [ "$BYOK_ENABLED" != "true" ] && [ "$BYOK_ENABLED" != "false" ]; then
    echo "ERROR: GENIE_BYOK_ENABLED must be 'true' or 'false' (got '$GENIE_BYOK_ENABLED')." >&2
    exit 1
fi

if [ -n "$UPDATE_CHECK_RAW" ] && [ -z "$UPDATE_CHECK" ]; then
    echo "WARNING: GENIE_UPDATE_CHECK='$UPDATE_CHECK_RAW' is not true or false; users will be asked on the Admin tab." >&2
fi

# ── Print config summary ─────────────────────────────────────────────────
_print_config() {
    echo "  ┌─ Configuration ─────────────────────────────────────────┐"
    echo "  │  Profile:      $PROFILE"
    echo "  │  App name:     $APP_NAME"
    echo "  │  Catalog:      $CATALOG"
    echo "  │  GSO Schema:   ${CATALOG}.${GSO_SCHEMA}"
    echo "  │  Warehouse ID: $WAREHOUSE_ID"
    echo "  │  Default LLM:  $LLM_MODEL"
    echo "  │  LLM route:    $LLM_ROUTE (BYOK: $BYOK_ENABLED)"
    if [ -n "$STATE_CATALOG" ]; then
        echo "  │  App state:    $STATE_CATALOG.$STATE_SCHEMA (Delta)"
    else
        echo "  │  App state:    Delta (location chosen in the app Setup dialog)"
    fi
    echo "  │  MLflow:       ${MLFLOW_EXPERIMENT_ID:-<disabled>}"
    echo "  │  Release check: ${UPDATE_CHECK:-ask users on the Admin tab}"
    echo "  └─────────────────────────────────────────────────────────┘"
}
