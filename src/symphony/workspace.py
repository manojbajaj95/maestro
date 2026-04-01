"""Workspace management and hooks."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .errors import WorkspaceError
from .models import Issue, PublishResult, ServiceConfig, WorkspaceHandle


TRANSIENT_DIRS = ("tmp", ".elixir_ls")


def _slugify(identifier: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in identifier).strip("-")


class WorkspaceManager:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.source_repo_root = self.config.workflow_path.parent.resolve()
        self.project_name = self._discover_project_name()
        base_root = self.config.workspace.root.expanduser().resolve()
        self.root = base_root / self.project_name

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
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._clone_workspace(path)
            created = True
        if not (path / ".git").exists():
            raise WorkspaceError("workspace_missing_git_clone")
        branch = await self._checkout_issue_branch(path, issue)
        await self._setup_workspace(path)
        if created:
            await self._run_hook(self.config.workspace.hooks.after_create, path, fatal=True)
        for name in TRANSIENT_DIRS:
            target = path / name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        return WorkspaceHandle(issue=issue, path=path, created=created, branch=branch)

    async def before_run(self, workspace: WorkspaceHandle) -> None:
        await self._run_hook(self.config.workspace.hooks.before_run, workspace.path, fatal=True)

    async def after_run(self, workspace: WorkspaceHandle) -> None:
        await self._run_hook(self.config.workspace.hooks.after_run, workspace.path, fatal=False)

    async def cleanup(self, workspace: WorkspaceHandle) -> None:
        await self._run_hook(self.config.workspace.hooks.before_remove, workspace.path, fatal=False)
        if workspace.path.exists():
            shutil.rmtree(workspace.path, ignore_errors=True)

    def _discover_project_name(self) -> str:
        try:
            result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=self.source_repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            remote = result.stdout.strip()
            if remote:
                if remote.startswith("git@"):
                    remote = remote.split(":", 1)[1]
                else:
                    remote = urlparse(remote).path.lstrip("/")
                repo_name = Path(remote).name.removesuffix(".git")
                if repo_name:
                    return repo_name
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return self.source_repo_root.name

    async def _clone_workspace(self, path: Path) -> None:
        await self._run_command(
            ["git", "clone", str(self.source_repo_root), str(path)],
            cwd=self.source_repo_root.parent,
            timeout_ms=self.config.workspace.hooks.timeout_ms,
        )

    async def publish_changes(self, workspace: WorkspaceHandle) -> PublishResult:
        branch = workspace.branch or f"codex/{_slugify(workspace.issue.identifier)}"
        if not await self._has_changes(workspace.path):
            return PublishResult(branch=branch, changed=False)
        await self._run_command(
            ["git", "add", "-A"],
            cwd=workspace.path,
            timeout_ms=self.config.workspace.hooks.timeout_ms,
        )
        commit_message = self._commit_message_for_issue(workspace.issue)
        await self._run_command(
            ["git", "commit", "-m", commit_message],
            cwd=workspace.path,
            timeout_ms=self.config.workspace.hooks.timeout_ms,
        )
        commit_sha = await self._run_command_output(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace.path,
            timeout_ms=self.config.workspace.hooks.timeout_ms,
        )
        await self._run_command(
            ["git", "push", "-u", "origin", branch],
            cwd=workspace.path,
            timeout_ms=self.config.workspace.hooks.timeout_ms * 4,
        )
        pr_url = await self._ensure_pull_request(workspace.path, workspace.issue, branch)
        return PublishResult(branch=branch, commit_sha=commit_sha, pr_url=pr_url, changed=True)

    async def _checkout_issue_branch(self, path: Path, issue: Issue) -> str:
        branch = f"codex/{_slugify(issue.identifier)}"
        exists = await self._run_command(
            ["git", "rev-parse", "--verify", branch],
            cwd=path,
            timeout_ms=self.config.workspace.hooks.timeout_ms,
            fatal=False,
        )
        if exists == 0:
            await self._run_command(
                ["git", "switch", branch],
                cwd=path,
                timeout_ms=self.config.workspace.hooks.timeout_ms,
            )
            return branch
        await self._run_command(
            ["git", "switch", "-c", branch],
            cwd=path,
            timeout_ms=self.config.workspace.hooks.timeout_ms,
        )
        return branch

    async def _setup_workspace(self, path: Path) -> None:
        if not (path / "pyproject.toml").exists():
            return
        await self._run_command(
            ["uv", "sync", "--group", "dev"],
            cwd=path,
            timeout_ms=self.config.workspace.hooks.timeout_ms * 4,
        )

    async def _run_hook(self, hook: str | None, cwd: Path, fatal: bool) -> None:
        if not hook:
            return
        proc = await asyncio.create_subprocess_shell(hook, cwd=str(cwd))
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=self.config.workspace.hooks.timeout_ms / 1000
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            if fatal:
                raise WorkspaceError("workspace_hook_timeout") from exc
            return
        if proc.returncode != 0 and fatal:
            raise WorkspaceError(f"workspace_hook_failed:{proc.returncode}")

    async def _has_changes(self, cwd: Path) -> bool:
        status = await self._run_command_output(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            timeout_ms=self.config.workspace.hooks.timeout_ms,
        )
        return bool(status.strip())

    async def _ensure_pull_request(self, cwd: Path, issue: Issue, branch: str) -> str:
        existing = await self._run_command_output(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
            cwd=cwd,
            timeout_ms=self.config.workspace.hooks.timeout_ms * 4,
        )
        try:
            payload = json.loads(existing or "[]")
        except json.JSONDecodeError as exc:
            raise WorkspaceError("workspace_invalid_gh_pr_list") from exc
        if payload:
            url = payload[0].get("url")
            if isinstance(url, str) and url:
                return url
        base = await self._default_branch(cwd)
        body = self._pull_request_body(issue)
        await self._run_command(
            [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--base",
                base,
                "--title",
                issue.title,
                "--body",
                body,
            ],
            cwd=cwd,
            timeout_ms=self.config.workspace.hooks.timeout_ms * 4,
        )
        created = await self._run_command_output(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
            cwd=cwd,
            timeout_ms=self.config.workspace.hooks.timeout_ms * 4,
        )
        try:
            payload = json.loads(created or "[]")
        except json.JSONDecodeError as exc:
            raise WorkspaceError("workspace_invalid_gh_pr_create") from exc
        if payload:
            url = payload[0].get("url")
            if isinstance(url, str) and url:
                return url
        raise WorkspaceError("workspace_pr_missing_after_create")

    async def _default_branch(self, cwd: Path) -> str:
        try:
            ref = await self._run_command_output(
                ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
                cwd=cwd,
                timeout_ms=self.config.workspace.hooks.timeout_ms,
            )
        except WorkspaceError:
            return "main"
        return ref.removeprefix("origin/").strip() or "main"

    def _commit_message_for_issue(self, issue: Issue) -> str:
        return f"chore: address {issue.identifier}"

    def _pull_request_body(self, issue: Issue) -> str:
        lines = [f"Closes {issue.identifier}", "", f"- Issue: {issue.title}"]
        if issue.url:
            lines.append(f"- Tracker URL: {issue.url}")
        return "\n".join(lines)

    async def _run_command(
        self,
        args: list[str],
        cwd: Path,
        timeout_ms: int,
        *,
        fatal: bool = True,
    ) -> int:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            if fatal:
                raise WorkspaceError(f"workspace_command_timeout:{args[0]}") from exc
            return 124
        if proc.returncode != 0 and fatal:
            message = stderr.decode().strip() or stdout.decode().strip() or str(proc.returncode)
            raise WorkspaceError(f"workspace_command_failed:{args[0]}:{message}")
        return proc.returncode if proc.returncode is not None else 0

    async def _run_command_output(
        self,
        args: list[str],
        cwd: Path,
        timeout_ms: int,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise WorkspaceError(f"workspace_command_timeout:{args[0]}") from exc
        if proc.returncode != 0:
            message = stderr.decode().strip() or stdout.decode().strip() or str(proc.returncode)
            raise WorkspaceError(f"workspace_command_failed:{args[0]}:{message}")
        return stdout.decode().strip()
