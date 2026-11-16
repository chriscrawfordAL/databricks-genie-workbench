"""
Genie Workbench - Main entry point.

Unified Databricks Genie Agent management platform combining:
- GenieRx: Deep LLM analysis, optimization suggestions
- GenieIQ: Org-wide IQ scoring, Delta-table persistence, admin dashboard
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local", override=True)
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _validate_mlflow_experiment() -> bool:
    """Validate that MLFLOW_EXPERIMENT_ID exists in the tracking server."""
    experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID", "").strip()

    if not experiment_id:
        logger.warning(
            "MLFLOW_EXPERIMENT_ID is not set. MLflow tracing will be disabled."
        )
        return False

    try:
        import mlflow

        tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)

        experiment = mlflow.get_experiment(experiment_id)
        if experiment is None:
            raise ValueError(f"Experiment {experiment_id} not found")

        logger.info(f"MLflow experiment validated: {experiment.name} (ID: {experiment_id})")
        return True

    except Exception as e:
        logger.warning(
            f"MLflow experiment ID '{experiment_id}' is not valid: {e}. "
            "MLflow tracing will be disabled."
        )
        os.environ.pop("MLFLOW_EXPERIMENT_ID", None)
        return False


_mlflow_configured = _validate_mlflow_experiment()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.app_version import read_version
from backend.services.auth import (
    clear_obo_user_token,
    clear_target_workspace,
    is_running_on_databricks_apps,
    set_obo_user_token,
    set_target_workspace,
)
from backend.routers.analysis import router as analysis_router
from backend.routers.spaces import router as spaces_router
from backend.routers.admin import router as admin_router
from backend.routers.app_info import router as app_info_router
from backend.routers.auth import router as auth_router
from backend.routers.create import router as create_router
from backend.routers.auto_optimize import router as auto_optimize_router
from backend.routers.setup import router as setup_router
from backend.routers.transfer import router as transfer_router
from backend.routers.workspaces import router as workspaces_router
from backend.watch.routers import (
    watch_admin_router,
    watch_cost_router,
    watch_feedback_router,
    watch_resources_router,
    watch_settings_router,
    watch_spaces_router,
    watch_traffic_gaps_router,
    watch_usage_router,
)
from backend.watch.services.system_tables import warm_cost_overview_cache


class OBOAuthMiddleware(BaseHTTPMiddleware):
    """Extract the user's access token and set a per-request OBO client.

    On Databricks Apps the platform forwards the user's OAuth token in the
    ``x-forwarded-access-token`` header (NOT the standard Authorization
    header).  We store it in a ContextVar so that every
    ``get_workspace_client()`` call in the request path returns a client
    authenticated as the user — not the service principal.

    Ref: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth#user-authorization

    For streaming endpoints (SSE), the ContextVar is NOT cleared after
    ``call_next`` because the response body streams lazily. Instead,
    streaming handlers must call ``set_obo_user_token`` themselves from
    within the generator (the token is stashed on ``request.state``).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request.state.target_workspace_id = ""
        if request.url.path.startswith("/api/"):
            token = request.headers.get("x-forwarded-access-token", "")
            if token:
                set_obo_user_token(token)
                logger.info("OBO: using user token for %s", request.url.path)
            else:
                logger.info("OBO: no x-forwarded-access-token, using SP for %s", request.url.path)
            request.state.user_token = token

            # Version Control observe surface: derive the per-request VC actor from
            # the Databricks Apps forwarded-identity headers, only when the observe
            # runtime is integrated (else the VC routers are not mounted at all). This
            # MUST run on the /api/ path — every VC route is under /api/version-control.
            observe = getattr(request.app.state, "vc_observe", None)
            if observe is not None:
                from backend.services.version_control.platform.app_observe import (
                    vc_auth_from_request,
                )

                vc_auth = vc_auth_from_request(
                    request.headers, observe.workspace_id,
                    dev_email=os.environ.get("DEV_USER_EMAIL"))
                if vc_auth is not None:
                    request.state.vc_auth = vc_auth

            rejection = await _apply_target_workspace(request)
            if rejection is not None:
                clear_obo_user_token()
                return rejection
        else:
            request.state.user_token = ""

        response = await call_next(request)

        is_streaming = getattr(response, "media_type", "") == "text/event-stream"
        if not is_streaming:
            clear_obo_user_token()
            clear_target_workspace()
        return response


# API prefixes that always run against the HOME workspace, whatever the caller
# selected: connection management + storage setup (home secrets/state), the
# caller's identity, app info, and Version Control (bound to the home
# workspace's control plane). Auto-Optimize keeps its data plane (GSO Delta
# tables, MLflow, the optimizer job) at home and opts into the remote agent
# client explicitly for Genie-Agent operations (see routers/auto_optimize.py).
_HOME_ONLY_PREFIXES = (
    "/api/workspaces",
    "/api/setup",
    "/api/transfer",
    "/api/auth",
    "/api/admin/version",
    "/api/admin/update-check",
    "/api/version-control",
    "/api/auto-optimize",
)


async def _apply_target_workspace(request: Request) -> Response | None:
    """Resolve ``X-Target-Workspace`` into a remote client for this request.

    Remote calls run as a service principal in the remote workspace (there is no
    per-user identity there), so targeting is restricted to home-workspace
    admins. A target that cannot be resolved is an error — never a silent
    fallback to home, which would show home data under a remote label.
    """
    conn_id = request.headers.get("x-target-workspace", "").strip()
    path = request.url.path
    if not conn_id or path.startswith(_HOME_ONLY_PREFIXES):
        return None
    from backend.services.auth import is_admin_request

    if not is_admin_request(request):
        return JSONResponse(
            status_code=403,
            content={"detail": "Only workspace admins can work in other workspaces."},
        )
    try:
        from backend.services import workspaces as ws_service

        client = await ws_service.get_client_for_workspace(conn_id)
        conn = await ws_service.get_connection(conn_id) if client is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not connect to target workspace %s", conn_id, exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"detail": f"Could not connect to the selected workspace: {str(e)[:200]}"},
        )
    if client is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "The selected workspace is no longer registered. Switch back to the home workspace."},
        )
    set_target_workspace(conn_id, client, (conn or {}).get("warehouse_id"))
    request.state.target_workspace_id = conn_id
    return None


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Delta state store: resolve the storage location and load state in the
    # background — a cold SQL warehouse can take minutes and must not stall the
    # Apps healthcheck. Reads serve from memory until the load completes.
    from backend.services import state_store
    state_init_task = asyncio.create_task(state_store.init())
    _ensure_gso_job_run_as()
    try:
        # Bug #2 schema probe — synchronous Delta SELECT (a few seconds worst
        # case on a cold warehouse). Run in a worker thread so we don't pin
        # the event loop during startup and stall healthcheck responses.
        from backend.routers.auto_optimize import probe_iterations_schema
        await asyncio.to_thread(probe_iterations_schema)
    except Exception:
        logger.warning("iterations schema probe failed", exc_info=True)

    # Pre-warm the Cost-tab overview cache in the background since it
    # takes a long time to run.
    asyncio.create_task(asyncio.to_thread(warm_cost_overview_cache, 7))

    # Periodically hand buffered cross-workspace transfer counters to the store.
    from backend.services import transfer_meter
    transfer_task = asyncio.create_task(transfer_meter.flush_periodically(30))

    yield

    transfer_task.cancel()
    await transfer_meter.flush()

    # Shutdown: flush pending state writes to Delta.
    if not state_init_task.done():
        state_init_task.cancel()
    await state_store.close()


app = FastAPI(
    title="Genie Workbench",
    description="Unified Databricks Genie Agent management platform",
    version=read_version(),
    lifespan=lifespan,
)

from backend.services.version_control.platform import compose
from backend.services.version_control.platform.app_observe import observe_config_from_env
from backend.services.version_control.platform.observe_seams import resolve_observe_runtime

app.state.version_control = compose()

# Version Control observe-only surface. Fully fail-closed: unless the trusted-workspace
# control-plane env vars are set (deploy-only), this resolves to None and no VC routes
# are mounted, so the app behaves exactly as before. See platform/app_observe.py.
app.state.vc_observe = resolve_observe_runtime(observe_config_from_env(os.environ))
if app.state.vc_observe is not None:
    logger.info("VC observe surface integrated (workspace=%s)", app.state.vc_observe.workspace_id)
else:
    logger.info("VC observe surface not configured; VC routes disabled")

if _mlflow_configured:
    try:
        from mlflow.genai.agent_server import setup_mlflow_git_based_version_tracking
        setup_mlflow_git_based_version_tracking()
    except Exception as e:
        logger.warning(f"MLflow git-based version tracking not configured: {e}")

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(OBOAuthMiddleware)

if not is_running_on_databricks_apps():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

def _ensure_gso_job_run_as() -> None:
    """Ensure the GSO optimization job's run_as matches this app's SP.

    Ported from the standalone GSO app's _JobRunAsBootstrap (app.py).
    The app runs as the SP, so it can update its own job's run_as
    without needing the "Service Principal User" role on the deployer.
    """
    job_id_str = os.environ.get("GSO_JOB_ID", "")
    if not job_id_str.isdigit():
        return
    try:
        from backend.services.auth import get_home_service_principal_client
        from genie_space_optimizer.backend.job_launcher import ensure_job_run_as

        ws = get_home_service_principal_client()
        sp_client_id = ws.config.client_id or os.environ.get("DATABRICKS_CLIENT_ID", "")
        if sp_client_id:
            ensure_job_run_as(ws, int(job_id_str), sp_client_id)
    except Exception:
        logger.warning("Could not verify GSO job run_as", exc_info=True)


# Mount all routers
app.include_router(analysis_router)
app.include_router(spaces_router)
app.include_router(admin_router)
app.include_router(app_info_router)
app.include_router(auth_router)
app.include_router(create_router)
app.include_router(auto_optimize_router)
app.include_router(setup_router)
app.include_router(workspaces_router)
app.include_router(transfer_router)

# GenieWatch (observability) — all routes under /api/watch/*
app.include_router(watch_spaces_router)
app.include_router(watch_traffic_gaps_router)
app.include_router(watch_cost_router)
app.include_router(watch_usage_router)
app.include_router(watch_feedback_router)
app.include_router(watch_resources_router)
app.include_router(watch_settings_router)
app.include_router(watch_admin_router)

# Version Control observe surface routers — only when the runtime is integrated.
if app.state.vc_observe is not None:
    from backend.services.version_control.platform.app_observe import mount_observe_routers

    mount_observe_routers(app, app.state.vc_observe)

# Serve static files from React build
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
FRONTEND_DIST_RESOLVED = FRONTEND_DIST.resolve()

if FRONTEND_DIST.exists():
    if (FRONTEND_DIST / "assets").exists():
        app.mount(
            "/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets"
        )

    _NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}

    @app.get("/")
    async def serve_root():
        return FileResponse(FRONTEND_DIST / "index.html", headers=_NO_CACHE)

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Serve static files from dist/ (e.g. favicon.svg) if they exist
        # .resolve() + .is_relative_to() prevents path traversal via encoded ..
        static_file = (FRONTEND_DIST / full_path).resolve()
        if static_file.is_file() and static_file.is_relative_to(FRONTEND_DIST_RESOLVED):
            return FileResponse(static_file)
        return FileResponse(FRONTEND_DIST / "index.html", headers=_NO_CACHE)

else:
    @app.get("/")
    async def serve_root_debug():
        return {
            "error": "Frontend not built or not deployed",
            "expected_path": str(FRONTEND_DIST),
            "hint": "Run: cd frontend && npm run build",
        }


def main():
    """Start the Genie Workbench server."""
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=not is_running_on_databricks_apps(),
    )


if __name__ == "__main__":
    main()
