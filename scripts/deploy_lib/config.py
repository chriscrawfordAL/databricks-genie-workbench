"""Configuration models for the notebook installer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


LLM_ROUTES = ("gateway", "classic")
RELEASE_CHECK_CHOICES = ("auto", "yes", "no")

APP_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
APP_NAME_ERROR = (
    "app_name must be lowercase letters, numbers, and hyphens, "
    "1-63 chars, and cannot start or end with a hyphen"
)
GSO_JOB_BASENAME = "gso-optimization-job"
DEFAULT_STATE_SCHEMA = "genie_workbench_state"
DEFAULT_WORKSPACES_SECRET_SCOPE = "genie-workbench-workspaces"
# Unquoted-safe Unity Catalog name: the installer interpolates these into SQL bare.
UC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UC_NAME_ERROR = "{field} must start with a letter or underscore and contain only letters, numbers, and underscores"


def default_gso_job_name(app_name: str) -> str:
    app_name = (app_name or "").strip()
    if not app_name:
        return GSO_JOB_BASENAME
    return f"{app_name}-{GSO_JOB_BASENAME}"


@dataclass(frozen=True)
class GsoJobInfo:
    job_id: int
    job_name: str
    notebooks_path: str
    wheel_path: str
    run_as_bound: bool = True


@dataclass(frozen=True)
class InstallConfig:
    app_name: str
    catalog: str
    warehouse_id: str
    llm_model: str = "databricks-claude-sonnet-4-6"
    llm_route: str = "gateway"
    byok_enabled: bool = False
    mlflow_experiment_id: str | None = None
    # Where the app keeps its Delta state table (<state_catalog>.<state_schema>.workbench_state).
    # Blank state_catalog = an admin chooses the location in the app's Setup dialog.
    state_catalog: str | None = None
    state_schema: str | None = None
    workspaces_secret_scope: str = DEFAULT_WORKSPACES_SECRET_SCOPE
    gso_schema: str = "genie_space_optimizer"
    repo_root: str | None = None
    deploy_workspace_path: str | None = None
    grant_genie_spaces: bool = False
    update_only: bool = False
    gso_job_name: str = ""
    gso_wheel_path: str | None = None
    release_check: str = "auto"

    def normalized(self) -> "InstallConfig":
        app_name = self.app_name.strip()
        state_catalog = (self.state_catalog or "").strip() or None
        # The schema only means something next to a catalog: blank catalog renders both empty.
        state_schema = ((self.state_schema or "").strip() or DEFAULT_STATE_SCHEMA) if state_catalog else None
        gso_job_name = (self.gso_job_name or "").strip() or default_gso_job_name(app_name)
        return InstallConfig(
            app_name=app_name,
            catalog=self.catalog.strip(),
            warehouse_id=self.warehouse_id.strip(),
            llm_model=(self.llm_model or "databricks-claude-sonnet-4-6").strip(),
            llm_route=(self.llm_route or "gateway").strip().lower(),
            byok_enabled=bool(self.byok_enabled),
            mlflow_experiment_id=(self.mlflow_experiment_id or "").strip() or None,
            state_catalog=state_catalog,
            state_schema=state_schema,
            workspaces_secret_scope=(self.workspaces_secret_scope or "").strip() or DEFAULT_WORKSPACES_SECRET_SCOPE,
            gso_schema=(self.gso_schema or "genie_space_optimizer").strip(),
            repo_root=(self.repo_root or "").strip() or None,
            deploy_workspace_path=(self.deploy_workspace_path or "").strip() or None,
            grant_genie_spaces=bool(self.grant_genie_spaces),
            update_only=bool(self.update_only),
            gso_job_name=gso_job_name,
            gso_wheel_path=(self.gso_wheel_path or "").strip() or None,
            release_check=(self.release_check or "auto").strip().lower(),
        )

    def validate(self) -> None:
        cfg = self.normalized()
        errors: list[str] = []
        if not APP_NAME_RE.match(cfg.app_name):
            errors.append(APP_NAME_ERROR)
        if not cfg.catalog:
            errors.append("catalog is required")
        if not cfg.warehouse_id:
            errors.append("warehouse_id is required")
        if cfg.llm_route not in LLM_ROUTES:
            errors.append("llm_route must be one of: gateway, classic")
        if cfg.state_catalog and not UC_NAME_RE.match(cfg.state_catalog):
            errors.append(UC_NAME_ERROR.format(field="state_catalog"))
        if cfg.state_schema and not UC_NAME_RE.match(cfg.state_schema):
            errors.append(UC_NAME_ERROR.format(field="state_schema"))
        if not cfg.workspaces_secret_scope:
            errors.append("workspaces_secret_scope is required")
        if not cfg.repo_root:
            errors.append("repo_root is required")
        if cfg.release_check not in RELEASE_CHECK_CHOICES:
            errors.append("release_check must be one of: auto, yes, no")
        if errors:
            raise ValueError("; ".join(errors))


@dataclass(frozen=True)
class InstallResult:
    app_name: str
    app_url: str | None
    source_path: str
    service_principal_client_id: str
    gso_job: GsoJobInfo
    state_location: str | None
    genie_spaces_granted: int
    deployment: dict[str, Any]
    verification: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
