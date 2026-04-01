"""Agent runner coordinating workspace, prompt rendering, and app-server sessions."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from .app_server import CodexAppServerClient
from .errors import AppServerError, TrackerError
from .models import HookWarnings, Issue, PublishResult, ServiceConfig, SessionResult
from .tracker import TrackerClient
from .workflow import render_prompt


@dataclass(slots=True)
class WorkerOutcome:
    normal_exit: bool
    issue: Issue
    result: SessionResult
    publish: PublishResult | None = None
    warnings: list[str] | None = None


class WorkspaceController(Protocol):
    async def prepare(self, issue: Issue): ...

    async def before_run(self, workspace) -> None: ...

    async def after_run(self, workspace) -> HookWarnings: ...

    async def publish_changes(self, workspace) -> PublishResult: ...


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
        session = None
        current = issue
        post_warnings: list[str] = []
        try:
            prompt = render_prompt(
                self.config.prompt_template, issue.model_dump(mode="json"), attempt
            )
            supported_tools = []
            if tool_handler is not None and self.config.tracker.kind == "linear":
                supported_tools.append({"name": "linear_graphql"})
            session = await self.app_server.start_session(
                workspace.path, supported_tools=supported_tools
            )
            result = await self.app_server.run_turns(
                session,
                prompt=prompt,
                max_turns=self.config.agent.max_turns,
                tool_handler=tool_handler,
            )
            refreshed = await self.tracker.fetch_issue_states_by_ids([issue.id])
            current = refreshed[0] if refreshed else issue
            publish = await self.workspace_manager.publish_changes(workspace)
        except (AppServerError, TrackerError, Exception) as exc:
            if session is not None:
                with suppress(Exception):
                    await session.stop()
            post_result = await self.workspace_manager.after_run(workspace)
            if post_result.warnings:
                detail = "; ".join(post_result.warnings)
                raise RuntimeError(f"{exc}; post_failed:{detail}") from exc
            raise
        if session is not None:
            with suppress(Exception):
                await session.stop()
        post_warnings = (await self.workspace_manager.after_run(workspace)).warnings
        return WorkerOutcome(
            normal_exit=True,
            issue=current,
            result=result,
            publish=publish,
            warnings=post_warnings,
        )
