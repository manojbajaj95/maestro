from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from symphony.models import (
    RunningEntry,
    ServiceConfig,
)
from symphony.orchestrator import RunnerProtocol, SymphonyOrchestrator, WorkspaceController
from symphony.server import build_app
from symphony.tracker import TrackerClient


class StubTracker:
    async def fetch_candidate_issues(self):
        return []

    async def fetch_issues_by_states(self, state_names):
        return []

    async def fetch_issue_states_by_ids(self, issue_ids):
        return []

    async def execute_raw_query(self, query, variables=None):
        return {}


class StubWorkspace:
    def workspace_path_for_issue(self, issue):
        return Path("/tmp")

    async def prepare(self, issue):
        raise AssertionError

    async def cleanup(self, handle):
        return None


class StubRunner:
    async def run_issue(self, issue, attempt, tool_handler=None):
        raise AssertionError


def make_orchestrator(tmp_path: Path) -> SymphonyOrchestrator:
    config = ServiceConfig.model_validate(
        {
            "workflow_path": tmp_path / "WORKFLOW.md",
            "prompt_template": "hello",
            "tracker": {"kind": "linear", "api_key": "token", "project_slug": "proj"},
            "workspace": {"root": tmp_path / "workspaces"},
        }
    )
    orchestrator = SymphonyOrchestrator(
        config,
        cast(TrackerClient, StubTracker()),
        cast(WorkspaceController, StubWorkspace()),
        cast(RunnerProtocol, StubRunner()),
        logger=__import__("logging").getLogger("test"),
    )
    orchestrator.state.running["1"] = RunningEntry(
        issue_id="1",
        identifier="ABC-1",
        attempt=1,
        started_at=datetime.now(timezone.utc),
        state_name="Todo",
        workspace_path=tmp_path,
    )
    return orchestrator


def test_server_state_and_dashboard(tmp_path: Path) -> None:
    client = TestClient(build_app(make_orchestrator(tmp_path)))
    state = client.get("/api/v1/state")
    assert state.status_code == 200
    assert state.json()["running"]["1"]["identifier"] == "ABC-1"
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "ABC-1" in dashboard.text
