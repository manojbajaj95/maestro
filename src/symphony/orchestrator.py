"""Polling orchestrator and runtime state management."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .errors import AppServerError, TrackerError
from .logging import log_event
from .models import Issue, RateLimitSnapshot, RetryEntry, RunningEntry, RuntimeSnapshot, ServiceConfig, UsageTotals
from .runner import WorkerOutcome
from .tracker import TrackerClient


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class RuntimeState:
    config: ServiceConfig
    running: dict[str, RunningEntry]
    claimed: set[str]
    retry_attempts: dict[str, RetryEntry]
    completed: set[str]
    errors: list[str]
    codex_totals_prompt: int = 0
    codex_totals_completion: int = 0
    codex_totals_all: int = 0
    codex_runtime_seconds: float = 0.0
    rate_limits: dict[str, int | None] | None = None

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            poll_interval_ms=self.config.polling.interval_ms,
            max_concurrent_agents=self.config.agent.max_concurrent_agents,
            running=self.running,
            claimed=sorted(self.claimed),
            retry_attempts=self.retry_attempts,
            completed=sorted(self.completed),
            codex_totals=UsageTotals(
                prompt_tokens=self.codex_totals_prompt,
                completion_tokens=self.codex_totals_completion,
                total_tokens=self.codex_totals_all,
                runtime_seconds=self.codex_runtime_seconds,
            ),
            codex_rate_limits=RateLimitSnapshot.model_validate(self.rate_limits or {}),
            errors=self.errors[-20:],
        )


class WorkspaceController(Protocol):
    def workspace_path_for_issue(self, issue: Issue): ...

    async def prepare(self, issue: Issue): ...

    async def cleanup(self, workspace) -> None: ...


class RunnerProtocol(Protocol):
    async def run_issue(self, issue: Issue, attempt: int, tool_handler: Any | None = None) -> WorkerOutcome: ...


class SymphonyOrchestrator:
    def __init__(
        self,
        config: ServiceConfig,
        tracker: TrackerClient,
        workspace_manager: WorkspaceController,
        runner: RunnerProtocol,
        logger,
    ) -> None:
        self.config = config
        self.tracker = tracker
        self.workspace_manager = workspace_manager
        self.runner = runner
        self.logger = logger
        self.state = RuntimeState(config, {}, set(), {}, set(), [])
        self._stop = asyncio.Event()

    async def startup_cleanup(self) -> None:
        terminal = await self.tracker.fetch_issues_by_states(self.config.tracker.terminal_states)
        for issue in terminal:
            with suppress(Exception):
                handle = await self.workspace_manager.prepare(issue)
                await self.workspace_manager.cleanup(handle)

    async def run_forever(self) -> None:
        await self.startup_cleanup()
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.polling.interval_ms / 1000)
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> None:
        await self._reconcile_running()
        issues = await self.tracker.fetch_candidate_issues()
        eligible = self._eligible(issues)
        available = self.config.agent.max_concurrent_agents - len(self.state.running)
        for issue in eligible[: max(available, 0)]:
            await self.dispatch_issue(issue, attempt=1)

    async def dispatch_issue(self, issue: Issue, attempt: int) -> None:
        if issue.id in self.state.claimed:
            return
        self.state.claimed.add(issue.id)
        workspace = self.workspace_manager.workspace_path_for_issue(issue)
        self.state.running[issue.id] = RunningEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=attempt,
            started_at=datetime.now(timezone.utc),
            state_name=issue.state.name,
            workspace_path=workspace,
        )
        log_event(self.logger, "issue_dispatched", issue_id=issue.id, issue_identifier=issue.identifier, attempt=attempt)
        asyncio.create_task(self._run_worker(issue, attempt))

    async def _run_worker(self, issue: Issue, attempt: int) -> None:
        try:
            outcome = await self.runner.run_issue(issue, attempt, tool_handler=self._handle_tool_call)
        except (AppServerError, TrackerError, Exception) as exc:
            self._on_worker_exit(issue, attempt, normal=False, error=str(exc))
            return
        self._apply_usage(outcome.result.usage, outcome.result.rate_limits.model_dump())
        current_state = outcome.issue.state.name
        terminal_names = {name.lower() for name in self.config.tracker.terminal_states}
        self._on_worker_exit(issue, attempt, normal=True, error=None)
        if current_state.lower() in terminal_names:
            with suppress(Exception):
                handle = await self.workspace_manager.prepare(outcome.issue)
                await self.workspace_manager.cleanup(handle)

    def _on_worker_exit(self, issue: Issue, attempt: int, normal: bool, error: str | None) -> None:
        self.state.running.pop(issue.id, None)
        if normal:
            self.state.completed.add(issue.id)
            self._schedule_retry(issue, 1, delay_ms=1_000, error=None)
        else:
            delay = min(10_000 * (2 ** max(attempt - 1, 0)), self.config.agent.max_retry_backoff_ms)
            self._schedule_retry(issue, attempt + 1, delay_ms=delay, error=error)
        log_event(
            self.logger,
            "worker_exit",
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            normal_exit=normal,
            error=error,
        )

    def _schedule_retry(self, issue: Issue, attempt: int, delay_ms: int, error: str | None) -> None:
        due_at = _now_ms() + delay_ms
        self.state.retry_attempts[issue.id] = RetryEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=attempt,
            due_at_ms=due_at,
            error=error,
        )
        with suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._wait_and_retry(issue.id))

    async def _wait_and_retry(self, issue_id: str) -> None:
        entry = self.state.retry_attempts.get(issue_id)
        if entry is None:
            return
        delay = max(entry.due_at_ms - _now_ms(), 0) / 1000
        await asyncio.sleep(delay)
        retry = self.state.retry_attempts.pop(issue_id, None)
        if retry is None:
            return
        issues = await self.tracker.fetch_candidate_issues()
        issue = next((item for item in issues if item.id == issue_id), None)
        if issue is None:
            self.state.claimed.discard(issue_id)
            return
        if len(self.state.running) >= self.config.agent.max_concurrent_agents:
            self._schedule_retry(issue, retry.attempt + 1, delay_ms=10_000, error="no_available_orchestrator_slots")
            return
        await self.dispatch_issue(issue, attempt=retry.attempt)

    async def _reconcile_running(self) -> None:
        if not self.state.running:
            return
        refreshed = await self.tracker.fetch_issue_states_by_ids(list(self.state.running))
        by_id = {issue.id: issue for issue in refreshed}
        active = {state.lower() for state in self.config.tracker.active_states}
        terminal = {state.lower() for state in self.config.tracker.terminal_states}
        for issue_id, entry in list(self.state.running.items()):
            issue = by_id.get(issue_id)
            if issue is None:
                continue
            state_name = issue.state.name.lower()
            self.state.running[issue_id].state_name = issue.state.name
            if state_name not in active:
                self.state.running.pop(issue_id, None)
                if state_name in terminal:
                    with suppress(Exception):
                        handle = await self.workspace_manager.prepare(issue)
                        await self.workspace_manager.cleanup(handle)

    def _eligible(self, issues: Iterable[Issue]) -> list[Issue]:
        active = {state.lower() for state in self.config.tracker.active_states}
        eligible: list[Issue] = []
        for issue in issues:
            if issue.id in self.state.claimed:
                continue
            if issue.state.name.lower() not in active:
                continue
            if issue.blocked_by:
                continue
            eligible.append(issue)
        eligible.sort(key=lambda item: (item.priority if item.priority is not None else 999_999, item.created_at))
        return eligible

    def _apply_usage(self, usage, rate_limits: dict[str, int | None]) -> None:
        self.state.codex_totals_prompt += usage.prompt_tokens
        self.state.codex_totals_completion += usage.completion_tokens
        self.state.codex_totals_all += usage.total_tokens
        self.state.codex_runtime_seconds += usage.runtime_seconds
        self.state.rate_limits = rate_limits

    async def _handle_tool_call(self, name: str, raw_input) -> dict[str, object]:
        if self.config.tracker.kind != "linear":
            return {"success": False, "error": "unsupported_tool_call"}
        if name != "linear_graphql":
            return {"success": False, "error": "unsupported_tool_call"}
        if isinstance(raw_input, str):
            query = raw_input
            variables = {}
        elif isinstance(raw_input, dict):
            query = raw_input.get("query", "")
            variables = raw_input.get("variables", {})
        else:
            return {"success": False, "error": "invalid_tool_input"}
        try:
            body = await self.tracker.execute_raw_query(query, variables)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "data": body}

    def stop(self) -> None:
        self._stop.set()
