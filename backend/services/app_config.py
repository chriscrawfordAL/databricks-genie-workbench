"""Where the Workbench keeps its Delta state (catalog + schema).

The location is chosen at runtime by a workspace admin in the first-run
**Setup** wizard (``routers/setup.py``). The choice is saved as a small JSON
file owned by the app service principal so it survives restarts and is not
tied to the admin who ran setup.

Resolution order:

1. The saved setup file (written by the wizard).
2. ``WORKBENCH_STATE_CATALOG`` + ``WORKBENCH_STATE_SCHEMA`` env vars (for
   automated installs that want to skip the wizard).

The setup file defaults to the service principal's own workspace home folder
(``/Users/<sp-application-id>/.genie-workbench/storage.json``), which the SP can
always write. ``WORKBENCH_STORAGE_CONFIG_PATH`` overrides it.

Only the location pointer lives here; the data itself lives in the
``<catalog>.<schema>.workbench_state`` Delta table (``services.delta_store``).
"""

from __future__ import annotations

import io
import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA = "genie_workbench_state"

# Location chosen in the setup wizard: {"catalog": ..., "schema": ...}.
_active: dict | None = None


def _env_location() -> tuple[str, str] | None:
    catalog = os.environ.get("WORKBENCH_STATE_CATALOG", "").strip()
    schema = os.environ.get("WORKBENCH_STATE_SCHEMA", "").strip()
    if catalog and schema and not catalog.startswith("__"):
        return catalog, schema
    return None


def location() -> tuple[str, str] | None:
    """The active ``(catalog, schema)``, or None until setup is complete."""
    if _active and _active.get("catalog") and _active.get("schema"):
        return _active["catalog"], _active["schema"]
    return _env_location()


def is_configured() -> bool:
    return location() is not None


def source() -> str:
    """Where the active location came from: ``setup``, ``env`` or ``none``."""
    if _active and _active.get("catalog") and _active.get("schema"):
        return "setup"
    return "env" if _env_location() else "none"


def set_active(catalog: str, schema: str) -> None:
    global _active
    _active = {"catalog": catalog, "schema": schema}


def clear_active() -> None:
    """Test helper: forget the in-process setup choice."""
    global _active
    _active = None


def suggested_location() -> tuple[str | None, str]:
    """A sensible default for the setup wizard (the GSO catalog, if known)."""
    catalog = os.environ.get("GSO_CATALOG", "").strip()
    if catalog.startswith("__"):
        catalog = ""
    return (catalog or None), DEFAULT_SCHEMA


# ── Setup file (read/written as the app service principal) ──────────────


def _sp():
    from backend.services.auth import get_home_service_principal_client

    return get_home_service_principal_client()


def config_path() -> str:
    explicit = os.environ.get("WORKBENCH_STORAGE_CONFIG_PATH", "").strip()
    if explicit:
        return explicit
    client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    if not client_id:
        try:
            client_id = (_sp().config.client_id or "").strip()
        except Exception:  # noqa: BLE001 - no SP outside Databricks Apps
            client_id = ""
    if not client_id:
        return ""
    return f"/Users/{client_id}/.genie-workbench/storage.json"


def load() -> bool:
    """Startup: read the saved setup file. Returns True when it set a location."""
    path = config_path()
    if not path:
        return False
    try:
        from databricks.sdk.service.workspace import ExportFormat

        resp = _sp().workspace.download(path, format=ExportFormat.AUTO)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - missing file is the normal first-run case
        logger.info("No saved storage setup at %s", path)
        return False
    catalog, schema = data.get("catalog"), data.get("schema")
    if catalog and schema:
        set_active(catalog, schema)
        logger.info("Workbench storage: %s.%s (from %s)", catalog, schema, path)
        return True
    logger.warning("Saved storage setup at %s is incomplete; setup is required", path)
    return False


def save(catalog: str, schema: str, configured_by: str | None = None) -> str:
    """Persist the chosen location as the SP. Returns the file path."""
    path = config_path()
    if not path:
        raise RuntimeError("Cannot determine where to save the storage setup")
    from databricks.sdk.service.workspace import ImportFormat

    client = _sp()
    parent = path.rsplit("/", 1)[0]
    try:
        client.workspace.mkdirs(parent)
    except Exception:  # noqa: BLE001 - may already exist
        logger.debug("mkdirs(%s) failed", parent, exc_info=True)
    payload = {"catalog": catalog, "schema": schema, "configured_by": configured_by}
    client.workspace.upload(
        path,
        io.BytesIO(json.dumps(payload, indent=2).encode("utf-8")),
        format=ImportFormat.AUTO,
        overwrite=True,
    )
    logger.info("Saved storage setup to %s", path)
    return path
