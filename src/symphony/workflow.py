"""Workflow and config loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from liquid import Environment, StrictUndefined

from .errors import ConfigError, WorkflowError
from .models import ServiceConfig, WorkflowDefinition


def resolve_runtime_path(raw: str | None, cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    if raw is None:
        return base / "WORKFLOW.md"
    return Path(raw).expanduser().resolve()


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    stripped = text.lstrip()
    if not stripped.startswith("---\n"):
        raise WorkflowError("missing_front_matter")
    lines = stripped.splitlines()
    try:
        second = lines[1:].index("---") + 1
    except ValueError as exc:
        raise WorkflowError("unterminated_front_matter") from exc
    front_matter = "\n".join(lines[1:second])
    body = "\n".join(lines[second + 1 :]).strip()
    try:
        data = yaml.safe_load(front_matter) or {}
    except yaml.YAMLError as exc:
        raise WorkflowError("invalid_yaml_front_matter") from exc
    if not isinstance(data, dict):
        raise WorkflowError("front_matter_must_be_map")
    return data, body


def load_workflow(path: Path) -> WorkflowDefinition:
    if not path.exists():
        raise WorkflowError(f"workflow_not_found:{path}")
    config, body = _split_front_matter(path.read_text())
    return WorkflowDefinition(config=config, prompt_template=body, path=path)


def _resolve_value(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("$"):
            env_name = value[1:]
            resolved = os.getenv(env_name)
            if resolved is None:
                raise ConfigError(f"missing_environment_variable:{env_name}")
            value = resolved
        if value.startswith("~"):
            return str(Path(value).expanduser())
        return value
    if isinstance(value, list):
        return [_resolve_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _resolve_value(item) for key, item in value.items()}
    return value


def build_service_config(workflow: WorkflowDefinition) -> ServiceConfig:
    resolved = _resolve_value(workflow.config)
    tracker = resolved.get("tracker")
    if not tracker:
        raise ConfigError("missing_tracker_config")
    if tracker.get("kind") not in {"linear", "github"}:
        raise ConfigError("unsupported_tracker_kind")
    workspace = resolved.get("workspace") or {}
    if "root" not in workspace:
        raise ConfigError("missing_workspace_root")
    payload = {
        "workflow_path": workflow.path,
        "prompt_template": workflow.prompt_template,
        "polling": resolved.get("polling") or {},
        "tracker": tracker,
        "workspace": workspace,
        "agent": resolved.get("agent") or {},
        "codex": resolved.get("codex") or {},
        "server": resolved.get("server") or {},
    }
    return ServiceConfig.model_validate(payload)


def render_prompt(template: str, issue: dict[str, Any], attempt: int) -> str:
    env = Environment(undefined=StrictUndefined)
    tpl = env.from_string(template)
    return tpl.render(issue=issue, attempt=attempt).strip()
