"""Workspaces router — manage remote workspace connections (admin-only).

The app is deployed in one home workspace but can target other workspaces in
the same account. This router lets a workspace admin register, test, update
and remove those connections. Secrets are handled by ``services.workspaces``
(stored in a home-workspace secret scope; never returned by the API).
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from backend.models import (
    WorkspaceConnection,
    WorkspaceConnectionCreate,
    WorkspaceConnectionUpdate,
    WorkspacesInfo,
    WorkspaceTestResult,
)
from backend.services import workspaces as ws_service
from backend.services.auth import get_home_databricks_host, is_admin_request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workspaces")


def _require_admin(request: Request) -> None:
    if not is_admin_request(request):
        raise HTTPException(status_code=403, detail="Workspace admin access is required to manage workspace connections.")


def _caller_email(request: Request) -> str | None:
    return (
        request.headers.get("X-Forwarded-Email")
        or request.headers.get("X-Forwarded-Preferred-Username")
        or request.headers.get("X-Forwarded-User")
        or os.environ.get("DEV_USER_EMAIL")
    )


@router.get("", response_model=list[WorkspaceConnection])
async def list_workspaces(request: Request) -> list[WorkspaceConnection]:
    """Registered remote workspaces (no secrets).

    Non-admins get an empty list (which hides the workspace switcher) rather
    than a 403, so the frontend's boot-time fetch stays quiet for them.
    """
    if not is_admin_request(request):
        return []
    return [WorkspaceConnection(**c) for c in await ws_service.list_connections()]


@router.get("/info", response_model=WorkspacesInfo)
async def workspaces_info(request: Request) -> WorkspacesInfo:
    """App identity details the admin needs to grant access in remote workspaces."""
    _require_admin(request)
    try:
        home = get_home_databricks_host()
    except Exception:  # noqa: BLE001
        home = None
    return WorkspacesInfo(
        app_sp_client_id=ws_service.app_sp_client_id() or None,
        home_host=home,
        secret_scope=ws_service.SECRET_SCOPE,
    )


@router.post("", response_model=WorkspaceConnection)
async def create_workspace(body: WorkspaceConnectionCreate, request: Request) -> WorkspaceConnection:
    """Register a remote workspace. Credentials are validated before anything is stored."""
    _require_admin(request)
    try:
        conn = await ws_service.create_connection(
            name=body.name,
            host=body.host,
            auth_mode=body.auth_mode,
            client_id=body.client_id,
            client_secret=body.client_secret,
            warehouse_id=body.warehouse_id,
            created_by=_caller_email(request),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to create workspace connection")
        raise HTTPException(status_code=502, detail=f"Could not connect to the workspace: {ws_service._short(e)}")
    return WorkspaceConnection(**conn)


@router.put("/{workspace_id}", response_model=WorkspaceConnection)
async def update_workspace(workspace_id: str, body: WorkspaceConnectionUpdate, request: Request) -> WorkspaceConnection:
    """Update a connection; rotates the stored secret if one is provided."""
    _require_admin(request)
    try:
        conn = await ws_service.update_connection(
            workspace_id,
            name=body.name,
            host=body.host,
            client_id=body.client_id,
            client_secret=body.client_secret,
            warehouse_id=body.warehouse_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace connection not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to update workspace connection")
        raise HTTPException(status_code=502, detail=f"Could not update the connection: {ws_service._short(e)}")
    return WorkspaceConnection(**conn)


@router.delete("/{workspace_id}")
async def delete_workspace(workspace_id: str, request: Request) -> dict:
    """Remove a connection and its stored secret."""
    _require_admin(request)
    await ws_service.delete_connection(workspace_id)
    return {"ok": True}


@router.post("/{workspace_id}/test", response_model=WorkspaceTestResult)
async def test_workspace(workspace_id: str, request: Request) -> WorkspaceTestResult:
    """Re-probe a stored connection and persist the fresh status."""
    _require_admin(request)
    try:
        result = await ws_service.test_connection(workspace_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace connection not found")
    return WorkspaceTestResult(**result)
