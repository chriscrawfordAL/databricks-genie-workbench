"""Data-transfer observability router.

Surfaces how much data the app has pulled IN from each remote (cross-workspace)
target, so an admin can estimate egress cost. The measurement is app-observed
byte counting at the outbound call sites (see ``services.transfer_meter``); it
is a proxy for the cloud provider's egress bill, not the billed figure.

Admin-only, mirroring the workspaces router: non-admins get an empty summary
(the tab is hidden) rather than a 403.
"""

import logging
from collections import defaultdict

from fastapi import APIRouter, Query, Request

from backend.models import (
    TransferByCategory,
    TransferDailyPoint,
    TransferSummary,
    WorkspaceTransfer,
)
from backend.services import state_store, transfer_meter
from backend.services import workspaces as ws_service
from backend.services.auth import is_admin_request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/transfer")


@router.get("/summary", response_model=TransferSummary)
async def transfer_summary(request: Request, days: int = Query(30, ge=1, le=365)) -> TransferSummary:
    """Per-workspace + daily rollup of data pulled in from remote workspaces.

    Admin-only: cross-workspace targeting is an admin capability, so non-admins
    get an empty summary (which hides the tab) rather than a 403.
    """
    if not is_admin_request(request):
        return TransferSummary(days=days)

    # Flush any buffered counters first so the view is current.
    await transfer_meter.flush()

    rows = await state_store.list_workspace_transfer(days=days)

    # Resolve workspace names/hosts from the registered connections (best-effort;
    # a since-deleted workspace just shows its id).
    names: dict[str, str] = {}
    hosts: dict[str, str] = {}
    try:
        for c in await ws_service.list_connections():
            names[c["workspace_id"]] = c.get("name")
            hosts[c["workspace_id"]] = c.get("host")
    except Exception:  # noqa: BLE001 - never fail the summary on a name lookup
        pass

    per_ws: dict[str, dict] = defaultdict(lambda: {
        "total_bytes": 0, "requests": 0,
        "by_category": {"genie": 0, "sql": 0, "system_tables": 0, "other": 0},
    })
    per_day: dict[str, int] = defaultdict(int)
    total_bytes = 0
    total_requests = 0

    for r in rows:
        wsid = r["workspace_id"]
        b = int(r.get("bytes") or 0)
        reqs = int(r.get("requests") or 0)
        cat = r.get("category") or "other"
        if cat not in ("genie", "sql", "system_tables", "other"):
            cat = "other"
        slot = per_ws[wsid]
        slot["total_bytes"] += b
        slot["requests"] += reqs
        slot["by_category"][cat] += b
        per_day[r["day"]] += b
        total_bytes += b
        total_requests += reqs

    workspaces = [
        WorkspaceTransfer(
            workspace_id=wsid,
            name=names.get(wsid),
            host=hosts.get(wsid),
            total_bytes=v["total_bytes"],
            requests=v["requests"],
            by_category=TransferByCategory(**v["by_category"]),
        )
        for wsid, v in per_ws.items()
    ]
    workspaces.sort(key=lambda w: w.total_bytes, reverse=True)

    daily = [TransferDailyPoint(day=day, bytes=b) for day, b in sorted(per_day.items())]

    return TransferSummary(
        days=days,
        total_bytes=total_bytes,
        total_requests=total_requests,
        workspaces=workspaces,
        daily=daily,
    )
