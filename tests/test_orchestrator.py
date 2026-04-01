from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from symphony.models import (
    Issue,
    IssueStateRef,
    RateLimitSnapshot,
    SessionResult,
    ServiceConfig,
    UsageTotals,
)
from symphony.orchestrator import RunnerProtocol, SymphonyOrchestrator, WorkspaceController
from symphony.runner import WorkerOutcome
from symphony.tracker import TrackerClient


class FakeTracker:
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        self.transitions: list[tuple[str, str]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        return self.issues

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        return []

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return [issue for issue in self.issues if issue.id in issue_ids]

    async def read_canonical_state(self, issue: Issue) -> str:
        mapping = {
            "todo": "to_do",
            "in progress": "in_progress",
            "in review": "in_review",
            "done": "done",
            "blocked": "blocked",
        }
        return mapping.get(issue.state.name.lower(), "unknown")

    async def move_to_in_progress(self, issue: Issue) -> Issue:
        self.transitions.append((issue.id, "in_progress"))
        updated = issue.model_copy(deep=True)
        updated.state.name = "In Progress"
        self._replace(updated)
        return updated

    async def move_to_in_review(self, issue: Issue) -> Issue:
        self.transitions.append((issue.id, "in_review"))
        updated = issue.model_copy(deep=True)
        updated.state.name = "In Review"
        self._replace(updated)
        return updated

    async def move_to_to_do(self, issue: Issue) -> Issue:
        self.transitions.append((issue.id, "to_do"))
        updated = issue.model_copy(deep=True)
        updated.state.name = "Todo"
        self._replace(updated)
        return updated

    async def execute_raw_query(self, query: str, variables: dict | None = None) -> dict:
        return {"ok": True}

    def _replace(self, updated: Issue) -> None:
        self.issues = [updated if issue.id == updated.id else issue for issue in self.issues]


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
    def __init__(self, should_fail: bool = True) -> None:
        self.should_fail = should_fail

    async def run_issue(self, issue: Issue, attempt: int, tool_handler=None):
        if self.should_fail:
            raise RuntimeError("boom")
        return WorkerOutcome(
            normal_exit=True,
            issue=issue,
            result=SessionResult(
                session_id="sess-1",
                turns_completed=1,
                usage=UsageTotals(total_tokens=1),
                rate_limits=RateLimitSnapshot(),
            ),
        )


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
                "states": {
                    "to_do": "Todo",
                    "in_progress": "In Progress",
                    "in_review": "In Review",
                    "done": "Done",
                    "blocked": "Blocked",
                },
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
    assert orchestrator.state.retry_attempts == {}


def test_dispatch_transitions_issue_to_in_progress_before_running(tmp_path: Path) -> None:
    issue = make_issue("1", "ABC-1", priority=1, created_at=datetime.now(timezone.utc))
    tracker = FakeTracker([issue])
    orchestrator = SymphonyOrchestrator(
        make_config(tmp_path),
        cast(TrackerClient, tracker),
        cast(WorkspaceController, FakeWorkspaceManager(tmp_path)),
        cast(RunnerProtocol, FakeRunner(should_fail=False)),
        logger=__import__("logging").getLogger("test"),
    )

    asyncio.run(orchestrator.dispatch_issue(issue, attempt=1))

    assert tracker.transitions[0] == ("1", "in_progress")


def test_worker_failure_moves_issue_back_to_to_do(tmp_path: Path) -> None:
    issue = make_issue("1", "ABC-1", priority=1, created_at=datetime.now(timezone.utc))
    tracker = FakeTracker([issue])
    orchestrator = SymphonyOrchestrator(
        make_config(tmp_path),
        cast(TrackerClient, tracker),
        cast(WorkspaceController, FakeWorkspaceManager(tmp_path)),
        cast(RunnerProtocol, FakeRunner(should_fail=True)),
        logger=__import__("logging").getLogger("test"),
    )

    asyncio.run(orchestrator._run_worker(issue, attempt=1))

    assert tracker.transitions[-1] == ("1", "to_do")
