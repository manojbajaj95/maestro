from __future__ import annotations

from pathlib import Path

import pytest

from symphony.errors import ConfigError, WorkflowError
from symphony.workflow import (
    build_service_config,
    load_workflow,
    render_prompt,
    resolve_runtime_path,
)


def test_default_workflow_path_uses_cwd(tmp_path: Path) -> None:
    assert resolve_runtime_path(None, cwd=tmp_path) == tmp_path / "WORKFLOW.md"


def test_missing_workflow_raises(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError):
        load_workflow(tmp_path / "WORKFLOW.md")


def test_load_and_build_service_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "token")
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        """---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: test
workspace:
  root: ~/tmp/symphony-workspaces
polling:
  interval_ms: 5000
server:
  port: 8080
---
Issue {{ issue.identifier }} attempt {{ attempt }}
"""
    )
    definition = load_workflow(workflow)
    config = build_service_config(definition)
    assert config.tracker.api_key == "token"
    assert str(config.workspace.root).endswith("symphony-workspaces")
    assert config.server.port == 8080
    assert (
        render_prompt(config.prompt_template, {"identifier": "ABC-1"}, 2) == "Issue ABC-1 attempt 2"
    )


def test_unknown_template_variable_fails() -> None:
    with pytest.raises(Exception):
        render_prompt("{{ missing }}", {"identifier": "ABC-1"}, 1)


def test_missing_env_var_fails(tmp_path: Path) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        """---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: test
workspace:
  root: /tmp/symphony
---
Prompt
"""
    )
    definition = load_workflow(workflow)
    with pytest.raises(ConfigError):
        build_service_config(definition)


def test_github_tracker_config_is_supported(tmp_path: Path) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        """---
tracker:
  kind: github
  assignee: "@me"
  states:
    to_do: status:todo
    in_progress: status:in-progress
    in_review: status:in-review
    blocked: status:blocked
workspace:
  root: /tmp/symphony
---
Prompt
"""
    )
    config = build_service_config(load_workflow(workflow))
    assert config.tracker.kind == "github"
    assert config.tracker.states.to_do == "status:todo"
    assert config.tracker.states.done == "closed"
