from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from symphony.models import Issue, IssueStateRef, ServiceConfig
from symphony.workspace import WorkspaceManager


def make_issue(identifier: str = "ABC-1") -> Issue:
    now = datetime.now(timezone.utc)
    return Issue(
        id="1",
        identifier=identifier,
        title="Test",
        state=IssueStateRef(name="Todo"),
        created_at=now,
        updated_at=now,
    )


def make_config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "workflow_path": tmp_path / "repo" / "WORKFLOW.md",
            "prompt_template": "hello",
            "tracker": {"kind": "linear", "api_key": "token", "project_slug": "proj"},
            "workspace": {"root": tmp_path / "symphony-home"},
        }
    )


class FakeWorkspaceManager(WorkspaceManager):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.calls: list[tuple[str, str, str]] = []
        self.project_name = "maestro"
        self.root = self.config.workspace.root.expanduser().resolve() / self.project_name

    async def _clone_workspace(self, path: Path) -> None:
        self.calls.append(("clone", "", str(path)))
        path.mkdir(parents=True, exist_ok=True)
        (path / ".git").mkdir()
        (path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

    async def _checkout_issue_branch(self, path: Path, issue: Issue) -> None:
        self.calls.append(("branch", issue.identifier, str(path)))

    async def _setup_workspace(self, path: Path) -> None:
        self.calls.append(("uv-sync", "", str(path)))


@pytest.mark.asyncio
async def test_workspace_prepare_clones_repo_and_runs_setup(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / "WORKFLOW.md").write_text(
        "---\ntracker:\n  kind: linear\n  api_key: token\n  project_slug: proj\nworkspace:\n  root: /tmp/x\n---\n"
    )
    manager = FakeWorkspaceManager(make_config(tmp_path))

    handle = await manager.prepare(make_issue())

    assert handle.created is True
    assert handle.path == tmp_path / "symphony-home" / "maestro" / "abc-1"
    assert [call[0] for call in manager.calls] == ["clone", "branch", "uv-sync"]


@pytest.mark.asyncio
async def test_workspace_prepare_reuses_existing_clone(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / "WORKFLOW.md").write_text(
        "---\ntracker:\n  kind: linear\n  api_key: token\n  project_slug: proj\nworkspace:\n  root: /tmp/x\n---\n"
    )
    manager = FakeWorkspaceManager(make_config(tmp_path))
    existing = tmp_path / "symphony-home" / "maestro" / "abc-1"
    existing.mkdir(parents=True)
    (existing / ".git").mkdir()
    (existing / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

    handle = await manager.prepare(make_issue())

    assert handle.created is False
    assert [call[0] for call in manager.calls] == ["branch", "uv-sync"]


@pytest.mark.asyncio
async def test_workspace_cleanup_removes_directory(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / "WORKFLOW.md").write_text(
        "---\ntracker:\n  kind: linear\n  api_key: token\n  project_slug: proj\nworkspace:\n  root: /tmp/x\n---\n"
    )
    manager = FakeWorkspaceManager(make_config(tmp_path))
    handle = await manager.prepare(make_issue())
    await manager.cleanup(handle)
    assert not handle.path.exists()
