"""Workspaces router — manage remote workspace connections.

Cross-workspace support: the app is deployed in one home workspace but can
target remote workspaces for deploy / score / monitor. This router lets an
admin register, test, and remove those connections. Secrets are handled by
``services.workspaces`` (stored in a home-workspace secret scope).
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from backend.models import (
    WorkspaceConnection,
    WorkspaceConnectionCreate,
    WorkspaceConnectionUpdate,
    WorkspaceTestResult,
)
from backend.services import workspaces as ws_service
from backend.services.auth import get_workspace_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workspaces")


def _is_admin_via_sdk() -> bool:
    """Check workspace-admin membership via SCIM using the current user's client.

    Databricks Apps does not reliably forward an ``X-Forwarded-Groups`` header,
    so header-only detection rejects real admins. As a robust fallback we read
    the caller's own SCIM record (via the OBO client) and look for membership in
    the workspace ``admins`` group.
    """
    try:
        me = get_workspace_client().current_user.me()
        for g in (me.groups or []):
            name = (getattr(g, "display", None) or getattr(g, "value", "") or "")
            if name.lower() == "admins":
                return True
    except Exception:
        logger.warning("SDK admin check failed", exc_info=True)
    return False


def _require_admin(request: Request) -> None:
    """Gate connection management to workspace admins.

    Admin is established by any of: the forwarded groups header containing
    ``admins`` (when present), SCIM group membership via the SDK, or the local
    DEV_ADMIN / DEV_USER_EMAIL escape hatches.
    """
    groups = (request.headers.get("X-Forwarded-Groups", "") or "").lower()
    is_admin = (
        "admins" in groups
        or os.environ.get("DEV_ADMIN", "").lower() == "true"
        or bool(os.environ.get("DEV_USER_EMAIL"))
        or _is_admin_via_sdk()
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required to manage workspace connections")


def _caller_email(request: Request) -> str | None:
    return (
        request.headers.get("X-Forwarded-User")
        or request.headers.get("X-Forwarded-Email")
        or os.environ.get("DEV_USER_EMAIL")
    )


@router.get("", response_model=list[WorkspaceConnection])
async def list_workspaces() -> list[WorkspaceConnection]:
    """List registered remote workspace connections (no secrets)."""
    conns = await ws_service.list_connections()
    return [WorkspaceConnection(**c) for c in conns]


@router.post("", response_model=WorkspaceConnection)
async def create_workspace(body: WorkspaceConnectionCreate, request: Request) -> WorkspaceConnection:
    """Register a remote workspace. Validates creds before persisting."""
    _require_admin(request)
    try:
        conn = await ws_service.create_connection(
            name=body.name,
            host=body.host,
            client_id=body.client_id,
            client_secret=body.client_secret,
            created_by=_caller_email(request),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to create workspace connection")
        raise HTTPException(status_code=500, detail=f"Failed to create connection: {e}")
    return WorkspaceConnection(**conn)


@router.put("/{workspace_id}", response_model=WorkspaceConnection)
async def update_workspace(
    workspace_id: str, body: WorkspaceConnectionUpdate, request: Request
) -> WorkspaceConnection:
    """Update a remote workspace connection; rotates the secret if provided."""
    _require_admin(request)
    try:
        conn = await ws_service.update_connection(
            workspace_id,
            name=body.name,
            host=body.host,
            client_id=body.client_id,
            client_secret=body.client_secret,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace connection not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to update workspace connection")
        raise HTTPException(status_code=500, detail=f"Failed to update connection: {e}")
    return WorkspaceConnection(**conn)


@router.delete("/{workspace_id}")
async def delete_workspace(workspace_id: str, request: Request) -> dict:
    """Remove a remote workspace connection and its stored secret."""
    _require_admin(request)
    try:
        await ws_service.delete_connection(workspace_id)
    except Exception as e:
        logger.exception("Failed to delete workspace connection")
        raise HTTPException(status_code=500, detail=f"Failed to delete connection: {e}")
    return {"ok": True}


@router.post("/{workspace_id}/test", response_model=WorkspaceTestResult)
async def test_workspace(workspace_id: str, request: Request) -> WorkspaceTestResult:
    """Re-probe a stored connection and persist the fresh status."""
    _require_admin(request)
    try:
        result = await ws_service.test_connection(workspace_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace connection not found")
    except Exception as e:
        logger.exception("Failed to test workspace connection")
        raise HTTPException(status_code=500, detail=f"Failed to test connection: {e}")
    return WorkspaceTestResult(**result)
