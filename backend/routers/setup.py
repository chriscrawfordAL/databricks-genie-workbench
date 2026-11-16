"""First-run storage setup — where the Workbench keeps its Delta state.

The Workbench stores its own state (scores, stars, settings, sessions,
GenieWatch caches, workspace connections) in a Unity Catalog Delta table,
``<catalog>.<schema>.workbench_state``. On first start no location is chosen;
a workspace admin picks one in the Setup dialog.

Browsing catalogs/schemas, creating the schema and granting the app service
principal access run with the **admin's own permissions** (OBO, as SQL on the
app's warehouse — only the ``sql`` user scope is needed). The service
principal then owns the state table and does all reads/writes.
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from backend.models import StorageSetupRequest, StorageStatus
from backend.services import app_config, delta_store, state_store
from backend.services.auth import get_home_workspace_client, is_admin_request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup")


def _require_admin(request: Request) -> None:
    if not is_admin_request(request):
        raise HTTPException(status_code=403, detail="Storage setup is available to workspace admins only.")


def _caller_email(request: Request) -> str | None:
    return (
        request.headers.get("X-Forwarded-Email")
        or request.headers.get("X-Forwarded-Preferred-Username")
        or os.environ.get("DEV_USER_EMAIL")
    )


def _status(request: Request) -> StorageStatus:
    st = state_store.status()
    cat, sch = app_config.suggested_location()
    return StorageStatus(
        configured=st["configured"],
        available=st["available"],
        catalog=st["catalog"],
        schema=st["schema"],
        source=st["source"],
        last_error=st["last_error"] if is_admin_request(request) else None,
        pending_writes=st["pending_writes"],
        is_admin=is_admin_request(request),
        suggested_catalog=cat,
        suggested_schema=sch,
    )


@router.get("/status", response_model=StorageStatus)
async def setup_status(request: Request) -> StorageStatus:
    """Whether storage is configured/usable and whether the caller may configure it."""
    return _status(request)


def _admin_sql(sql: str) -> list[dict]:
    """Run SQL on the app's warehouse with the admin's own token (home)."""
    return delta_store.run_statement(sql, client=get_home_workspace_client())


@router.get("/catalogs")
async def setup_catalogs(request: Request) -> list[dict]:
    """Catalogs the admin can see."""
    _require_admin(request)
    try:
        rows = _admin_sql("SHOW CATALOGS")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not list catalogs: {e}")
    names = sorted({str(r.get("catalog") or next(iter(r.values()), "")) for r in rows} - {"", "system", "samples"})
    return [{"name": n} for n in names]


@router.get("/schemas")
async def setup_schemas(request: Request, catalog: str) -> list[dict]:
    """Schemas in a catalog the admin can see."""
    _require_admin(request)
    if not catalog:
        raise HTTPException(status_code=422, detail="catalog is required")
    try:
        rows = _admin_sql(f"SHOW SCHEMAS IN {delta_store.quote_ident(catalog)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not list schemas: {e}")
    names = sorted(
        {str(r.get("databaseName") or r.get("namespace") or next(iter(r.values()), "")) for r in rows}
        - {"", "information_schema"}
    )
    return [{"name": n} for n in names]


def _sp_principal() -> str:
    from backend.services.workspaces import app_sp_client_id

    sp = app_sp_client_id()
    if not sp:
        try:
            from backend.services.auth import get_home_service_principal_client

            sp = (get_home_service_principal_client().config.client_id or "").strip()
        except Exception:  # noqa: BLE001
            sp = ""
    return sp


@router.post("/configure", response_model=StorageStatus)
async def configure_storage(request: Request, body: StorageSetupRequest) -> StorageStatus:
    """Create/grant the chosen schema, save the choice, and bring storage up."""
    _require_admin(request)
    catalog, schema = body.catalog.strip(), body.schema_name.strip()
    if not catalog or not schema:
        raise HTTPException(status_code=422, detail="catalog and schema are required")
    if not delta_store.warehouse_id():
        raise HTTPException(status_code=500, detail="No SQL warehouse is attached to the app.")
    sp = _sp_principal()
    if not sp:
        raise HTTPException(status_code=500, detail="Could not resolve the app service principal.")

    cat_q = delta_store.quote_ident(catalog)
    schema_q = f"{cat_q}.{delta_store.quote_ident(schema)}"
    sp_q = delta_store.quote_ident(sp)
    if body.create_schema:
        try:
            _admin_sql(f"CREATE SCHEMA IF NOT EXISTS {schema_q} COMMENT 'Genie Workbench app state'")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Could not create schema {catalog}.{schema}: {e}")
    try:
        _admin_sql(f"GRANT USE CATALOG ON CATALOG {cat_q} TO {sp_q}")
        _admin_sql(f"GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA {schema_q} TO {sp_q}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"Could not grant the app service principal access to {catalog}.{schema}: {e}",
        )

    previous = app_config.location()
    app_config.set_active(catalog, schema)
    try:
        await state_store.reconfigure()
    except Exception as e:  # noqa: BLE001 - roll back to the previous location
        if previous:
            app_config.set_active(*previous)
        else:
            app_config.clear_active()
        raise HTTPException(status_code=400, detail=f"The app could not create its state table: {e}")
    try:
        app_config.save(catalog, schema, configured_by=_caller_email(request))
    except Exception as e:  # noqa: BLE001 - running now, but warn it won't survive a restart
        logger.warning("Failed to save storage setup: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Storage is working, but the choice could not be saved for restarts: {e}",
        )
    return _status(request)
