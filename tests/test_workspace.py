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
            "workflow_path": tmp_path / "WORKFLOW.md",
            "prompt_template": "hello",
            "tracker": {"kind": "linear", "api_key": "token", "project_slug": "proj"},
            "workspace": {"root": tmp_path / "workspaces"},
        }
    )


@pytest.mark.asyncio
async def test_workspace_prepare_creates_and_reuses(tmp_path: Path) -> None:
    manager = WorkspaceManager(make_config(tmp_path))
    issue = make_issue()
    first = await manager.prepare(issue)
    second = await manager.prepare(issue)
    assert first.created is True
    assert second.created is False
    assert second.path.exists()


@pytest.mark.asyncio
async def test_workspace_cleanup_removes_directory(tmp_path: Path) -> None:
    manager = WorkspaceManager(make_config(tmp_path))
    handle = await manager.prepare(make_issue())
    await manager.cleanup(handle)
    assert not handle.path.exists()
