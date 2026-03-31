"""Agent runner coordinating workspace, prompt rendering, and app-server sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .app_server import CodexAppServerClient
from .errors import AppServerError, TrackerError
from .models import Issue, ServiceConfig, SessionResult
from .tracker import TrackerClient
from .workflow import render_prompt


@dataclass(slots=True)
class WorkerOutcome:
    normal_exit: bool
    issue: Issue
    result: SessionResult


class WorkspaceController(Protocol):
    async def prepare(self, issue: Issue): ...

    async def before_run(self, workspace) -> None: ...

    async def after_run(self, workspace) -> None: ...


class AgentRunner:
    def __init__(
        self,
        config: ServiceConfig,
        tracker: TrackerClient,
        workspace_manager: WorkspaceController,
        app_server: CodexAppServerClient,
    ) -> None:
        self.config = config
        self.tracker = tracker
        self.workspace_manager = workspace_manager
        self.app_server = app_server

    async def run_issue(
        self,
        issue: Issue,
        attempt: int,
        tool_handler: Any | None = None,
    ) -> WorkerOutcome:
        workspace = await self.workspace_manager.prepare(issue)
        await self.workspace_manager.before_run(workspace)
        try:
            prompt = render_prompt(self.config.prompt_template, issue.model_dump(mode="json"), attempt)
            supported_tools = []
            if tool_handler is not None and self.config.tracker.kind == "linear":
                supported_tools.append({"name": "linear_graphql"})
            session = await self.app_server.start_session(workspace.path, supported_tools=supported_tools)
            result = await self.app_server.run_turns(
                session,
                prompt=prompt,
                max_turns=self.config.agent.max_turns,
                tool_handler=tool_handler,
            )
            refreshed = await self.tracker.fetch_issue_states_by_ids([issue.id])
            current = refreshed[0] if refreshed else issue
            await session.stop()
            await self.workspace_manager.after_run(workspace)
            return WorkerOutcome(normal_exit=True, issue=current, result=result)
        except (AppServerError, TrackerError):
            await self.workspace_manager.after_run(workspace)
            raise
