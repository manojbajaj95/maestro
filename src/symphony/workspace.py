"""Workspace management and hooks."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .errors import WorkspaceError
from .models import Issue, ServiceConfig, WorkspaceHandle


TRANSIENT_DIRS = ("tmp", ".elixir_ls")


def _slugify(identifier: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in identifier).strip("-")


class WorkspaceManager:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.root = self.config.workspace.root.expanduser().resolve()

    def workspace_path_for_issue(self, issue: Issue) -> Path:
        candidate = (self.root / _slugify(issue.identifier)).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise WorkspaceError("workspace_out_of_root")
        return candidate

    async def prepare(self, issue: Issue) -> WorkspaceHandle:
        path = self.workspace_path_for_issue(issue)
        if path.exists() and not path.is_dir():
            raise WorkspaceError("workspace_path_is_not_directory")
        created = False
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created = True
            await self._run_hook(self.config.workspace.hooks.after_create, path, fatal=True)
        for name in TRANSIENT_DIRS:
            target = path / name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        return WorkspaceHandle(issue=issue, path=path, created=created)

    async def before_run(self, workspace: WorkspaceHandle) -> None:
        await self._run_hook(self.config.workspace.hooks.before_run, workspace.path, fatal=True)

    async def after_run(self, workspace: WorkspaceHandle) -> None:
        await self._run_hook(self.config.workspace.hooks.after_run, workspace.path, fatal=False)

    async def cleanup(self, workspace: WorkspaceHandle) -> None:
        await self._run_hook(self.config.workspace.hooks.before_remove, workspace.path, fatal=False)
        if workspace.path.exists():
            shutil.rmtree(workspace.path, ignore_errors=True)

    async def _run_hook(self, hook: str | None, cwd: Path, fatal: bool) -> None:
        if not hook:
            return
        proc = await asyncio.create_subprocess_shell(hook, cwd=str(cwd))
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.config.workspace.hooks.timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            if fatal:
                raise WorkspaceError("workspace_hook_timeout") from exc
            return
        if proc.returncode != 0 and fatal:
            raise WorkspaceError(f"workspace_hook_failed:{proc.returncode}")
