from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from symphony.models import (
    Issue,
    IssueStateRef,
    RetryEntry,
    ServiceConfig,
)
from symphony.orchestrator import RunnerProtocol, SymphonyOrchestrator, WorkspaceController
from symphony.tracker import TrackerClient


class FakeTracker:
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues

    async def fetch_candidate_issues(self) -> list[Issue]:
        return self.issues

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        return []

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return [issue for issue in self.issues if issue.id in issue_ids]

    async def execute_raw_query(self, query: str, variables: dict | None = None) -> dict:
        return {"ok": True}


class FakeWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def prepare(self, issue: Issue):
        class Handle:
            def __init__(self, path: Path) -> None:
                self.path = path

        return Handle(self.root / issue.identifier)

    async def cleanup(self, handle) -> None:
        return None

    def workspace_path_for_issue(self, issue: Issue) -> Path:
        return self.root / issue.identifier


class FakeRunner:
    async def run_issue(self, issue: Issue, attempt: int, tool_handler=None):
        raise RuntimeError("boom")


def make_issue(
    issue_id: str,
    identifier: str,
    *,
    priority: int,
    created_at: datetime,
    blockers: list[str] | None = None,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=identifier,
        priority=priority,
        state=IssueStateRef(name="Todo"),
        blocked_by=blockers or [],
        created_at=created_at,
        updated_at=created_at,
    )


def make_config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "workflow_path": tmp_path / "WORKFLOW.md",
            "prompt_template": "hello",
            "tracker": {
                "kind": "linear",
                "api_key": "token",
                "project_slug": "proj",
                "terminal_states": ["Done", "Canceled"],
            },
            "workspace": {"root": tmp_path / "workspaces"},
            "agent": {"max_concurrent_agents": 1, "max_retry_backoff_ms": 60000},
        }
    )


def test_eligible_sorts_by_priority_then_age(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    issues = [
        make_issue("2", "ABC-2", priority=2, created_at=now - timedelta(hours=1)),
        make_issue("1", "ABC-1", priority=1, created_at=now),
        make_issue("3", "ABC-3", priority=1, created_at=now - timedelta(hours=2)),
    ]
    orchestrator = SymphonyOrchestrator(
        make_config(tmp_path),
        cast(TrackerClient, FakeTracker(issues)),
        cast(WorkspaceController, FakeWorkspaceManager(tmp_path)),
        cast(RunnerProtocol, FakeRunner()),
        logger=__import__("logging").getLogger("test"),
    )
    eligible = orchestrator._eligible(issues)
    assert [issue.identifier for issue in eligible] == ["ABC-3", "ABC-1", "ABC-2"]


def test_non_terminal_blockers_skip_todo_issue(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    issue = make_issue("1", "ABC-1", priority=1, created_at=now, blockers=["ABC-0"])
    orchestrator = SymphonyOrchestrator(
        make_config(tmp_path),
        cast(TrackerClient, FakeTracker([issue])),
        cast(WorkspaceController, FakeWorkspaceManager(tmp_path)),
        cast(RunnerProtocol, FakeRunner()),
        logger=__import__("logging").getLogger("test"),
    )
    assert orchestrator._eligible([issue]) == []


def test_retry_backoff_caps(tmp_path: Path) -> None:
    issue = make_issue("1", "ABC-1", priority=1, created_at=datetime.now(timezone.utc))
    orchestrator = SymphonyOrchestrator(
        make_config(tmp_path),
        cast(TrackerClient, FakeTracker([issue])),
        cast(WorkspaceController, FakeWorkspaceManager(tmp_path)),
        cast(RunnerProtocol, FakeRunner()),
        logger=__import__("logging").getLogger("test"),
    )
    orchestrator._schedule_retry(
        issue, attempt=10, delay_ms=min(10_000 * (2**8), 60_000), error="boom"
    )
    retry = orchestrator.state.retry_attempts["1"]
    assert isinstance(retry, RetryEntry)
    assert retry.attempt == 10
