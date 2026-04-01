"""Typed domain models used across the service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IssueStateRef(BaseModel):
    id: str | None = None
    name: str
    type: str | None = None


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    identifier: str
    title: str
    url: str | None = None
    description: str | None = None
    priority: int | None = None
    state: IssueStateRef
    labels: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: Any) -> list[str]:
        if not value:
            return []
        return [str(item).lower() for item in value]


class WorkflowDefinition(BaseModel):
    config: dict[str, Any]
    prompt_template: str
    path: Path


class PollingConfig(BaseModel):
    interval_ms: int = 30_000


class TrackerConfig(BaseModel):
    class StatesConfig(BaseModel):
        to_do: str | None = None
        in_progress: str | None = None
        in_review: str | None = None
        done: str | None = None
        blocked: str | None = None

    kind: Literal["linear", "github"]
    endpoint: str = "https://api.linear.app/graphql"
    api_key: str | None = None
    project_slug: str | None = None
    assignee: str | None = None
    states: StatesConfig = Field(default_factory=StatesConfig)
    page_size: int = 50
    timeout_ms: int = 30_000

    @model_validator(mode="after")
    def apply_kind_defaults(self) -> "TrackerConfig":
        if self.kind == "linear":
            if not self.api_key:
                raise ValueError("linear_api_key_required")
            if not self.project_slug:
                raise ValueError("linear_project_slug_required")
            self.states.to_do = self.states.to_do or "Todo"
            self.states.in_progress = self.states.in_progress or "In Progress"
            self.states.in_review = self.states.in_review or "In Review"
            self.states.done = self.states.done or "Done"
            self.states.blocked = self.states.blocked or "Blocked"
        else:
            self.states.to_do = self.states.to_do or "status:todo"
            self.states.in_progress = self.states.in_progress or "status:in-progress"
            self.states.in_review = self.states.in_review or "status:in-review"
            self.states.done = self.states.done or "closed"
            self.states.blocked = self.states.blocked or "status:blocked"
        return self


class WorkspaceHooks(BaseModel):
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60_000


class WorkspaceConfig(BaseModel):
    root: Path
    hooks: WorkspaceHooks = Field(default_factory=WorkspaceHooks)


class AgentConfig(BaseModel):
    max_concurrent_agents: int = 1
    max_concurrent_agents_by_state: dict[str, int] = Field(default_factory=dict)
    max_retry_backoff_ms: int = 300_000
    max_turns: int = 20

    @field_validator("max_concurrent_agents_by_state", mode="before")
    @classmethod
    def normalize_state_limits(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, int] = {}
        for key, raw in value.items():
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                normalized[str(key).strip().lower()] = parsed
        return normalized


class CodexConfig(BaseModel):
    command: str = "codex app-server"
    read_timeout_ms: int = 10_000
    turn_timeout_ms: int = 300_000
    stall_timeout_ms: int = 300_000
    approval_policy: str = "on-failure"
    sandbox_mode: str = "workspace-write"


class ServerConfig(BaseModel):
    port: int | None = None
    host: str = "127.0.0.1"


class ServiceConfig(BaseModel):
    workflow_path: Path
    polling: PollingConfig = Field(default_factory=PollingConfig)
    tracker: TrackerConfig
    workspace: WorkspaceConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    prompt_template: str


class RetryEntry(BaseModel):
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: int
    error: str | None = None


class UsageTotals(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    runtime_seconds: float = 0.0


class RateLimitSnapshot(BaseModel):
    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    reset_seconds: int | None = None


class RunningEntry(BaseModel):
    issue_id: str
    identifier: str
    session_id: str | None = None
    attempt: int = 1
    started_at: datetime
    state_name: str
    workspace_path: Path


class RuntimeSnapshot(BaseModel):
    poll_interval_ms: int
    max_concurrent_agents: int
    running: dict[str, RunningEntry] = Field(default_factory=dict)
    claimed: list[str] = Field(default_factory=list)
    retry_attempts: dict[str, RetryEntry] = Field(default_factory=dict)
    completed: list[str] = Field(default_factory=list)
    codex_totals: UsageTotals = Field(default_factory=UsageTotals)
    codex_rate_limits: RateLimitSnapshot = Field(default_factory=RateLimitSnapshot)
    errors: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class WorkspaceHandle:
    issue: Issue
    path: Path
    created: bool
    branch: str | None = None


@dataclass(slots=True)
class SessionResult:
    session_id: str | None
    turns_completed: int
    usage: UsageTotals = field(default_factory=UsageTotals)
    rate_limits: RateLimitSnapshot = field(default_factory=RateLimitSnapshot)
    normal_exit: bool = True
    error: str | None = None


@dataclass(slots=True)
class PublishResult:
    branch: str
    commit_sha: str | None = None
    pr_url: str | None = None
    changed: bool = False
