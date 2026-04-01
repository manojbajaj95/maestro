from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

from symphony.errors import AppServerError, WorkspaceError
from symphony.models import (
    HookWarnings,
    Issue,
    IssueStateRef,
    RateLimitSnapshot,
    ServiceConfig,
    SessionResult,
    UsageTotals,
    WorkspaceHandle,
)
from symphony.app_server import CodexAppServerClient
from symphony.runner import AgentRunner
from symphony.tracker import TrackerClient


def make_issue() -> Issue:
    now = datetime.now(timezone.utc)
    return Issue(
        id="1",
        identifier="ABC-1",
        title="Test issue",
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


class FakeSession:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def stop(self) -> None:
        self.calls.append("stop_session")


class FakeAppServer:
    def __init__(self, calls: list[str], *, should_fail: bool = False) -> None:
        self.calls = calls
        self.should_fail = should_fail

    async def start_session(self, path: Path, supported_tools=None):
        self.calls.append("start_session")
        return FakeSession(self.calls)

    async def run_turns(self, session, prompt: str, max_turns: int, tool_handler=None):
        self.calls.append("run_turns")
        if self.should_fail:
            raise AppServerError("boom")
        return SessionResult(
            session_id="sess-1",
            turns_completed=1,
            usage=UsageTotals(total_tokens=1),
            rate_limits=RateLimitSnapshot(),
        )


class FakeTracker:
    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return [make_issue()]


class FakeWorkspaceManager:
    def __init__(self, tmp_path: Path, calls: list[str], post_warnings: list[str] | None = None):
        self.calls = calls
        self.post_warnings = post_warnings or []
        self.handle = WorkspaceHandle(
            issue=make_issue(),
            path=tmp_path / "workspaces" / "abc-1",
            created=False,
            branch="codex/abc-1",
        )
        self.handle.path.mkdir(parents=True, exist_ok=True)

    async def prepare(self, issue: Issue):
        self.calls.append("prepare_workspace")
        return self.handle

    async def before_run(self, workspace) -> None:
        self.calls.append("before_run")

    async def after_run(self, workspace) -> HookWarnings:
        self.calls.append("after_run")
        return HookWarnings(warnings=list(self.post_warnings))

    async def publish_changes(self, workspace):
        self.calls.append("publish_changes")
        return type("Publish", (), {"branch": "codex/abc-1", "changed": False})()


@pytest.mark.asyncio
async def test_run_issue_runs_post_after_publish_and_surfaces_warnings(tmp_path: Path) -> None:
    calls: list[str] = []
    runner = AgentRunner(
        make_config(tmp_path),
        cast(TrackerClient, FakeTracker()),
        FakeWorkspaceManager(tmp_path, calls, post_warnings=["post_failed:cleanup"]),
        cast(CodexAppServerClient, FakeAppServer(calls)),
    )

    outcome = await runner.run_issue(make_issue(), attempt=1)

    assert outcome.warnings == ["post_failed:cleanup"]
    assert calls.index("publish_changes") < calls.index("after_run")


@pytest.mark.asyncio
async def test_run_issue_runs_post_on_failure(tmp_path: Path) -> None:
    calls: list[str] = []
    runner = AgentRunner(
        make_config(tmp_path),
        cast(TrackerClient, FakeTracker()),
        FakeWorkspaceManager(tmp_path, calls),
        cast(CodexAppServerClient, FakeAppServer(calls, should_fail=True)),
    )

    with pytest.raises(AppServerError):
        await runner.run_issue(make_issue(), attempt=1)

    assert "after_run" in calls


@pytest.mark.asyncio
async def test_run_issue_aborts_when_prepare_fails(tmp_path: Path) -> None:
    calls: list[str] = []

    class FailingWorkspaceManager(FakeWorkspaceManager):
        async def before_run(self, workspace) -> None:
            self.calls.append("before_run")
            raise WorkspaceError("workspace_prepare_failed")

    runner = AgentRunner(
        make_config(tmp_path),
        cast(TrackerClient, FakeTracker()),
        FailingWorkspaceManager(tmp_path, calls),
        cast(CodexAppServerClient, FakeAppServer(calls)),
    )

    with pytest.raises(WorkspaceError):
        await runner.run_issue(make_issue(), attempt=1)

    assert "start_session" not in calls
