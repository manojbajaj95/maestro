"""Codex app-server subprocess client."""

from __future__ import annotations

import asyncio
import contextlib
import json
from asyncio.subprocess import Process
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AppServerError
from .models import CodexConfig, RateLimitSnapshot, SessionResult, UsageTotals


@dataclass(slots=True)
class SessionEvent:
    type: str
    payload: dict[str, Any]


class AppServerSession:
    def __init__(self, process: Process, config: CodexConfig) -> None:
        self.process = process
        self.config = config
        self.session_id: str | None = None
        self.thread_id: str | None = None
        self.usage = UsageTotals()
        self.rate_limits = RateLimitSnapshot()
        self.stderr_open = True

    async def request(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        message = json.dumps(payload) + "\n"
        self.process.stdin.write(message.encode())
        await self.process.stdin.drain()

    async def stop(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self.process.terminate()
        await self.process.wait()


class CodexAppServerClient:
    def __init__(self, config: CodexConfig) -> None:
        self.config = config

    async def start_session(self, cwd: Path, supported_tools: list[dict[str, Any]] | None = None) -> AppServerSession:
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            self.config.command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        session = AppServerSession(process, self.config)
        await session.request(
            {
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "symphony", "version": "0.1.0"},
                    "capabilities": {"tools": supported_tools or []},
                    "approvalPolicy": self.config.approval_policy,
                    "sandboxMode": self.config.sandbox_mode,
                },
            }
        )
        await session.request({"method": "initialized", "params": {}})
        await session.request({"id": "thread/start", "method": "thread/start", "params": {}})
        return session

    async def run_turns(
        self,
        session: AppServerSession,
        prompt: str,
        max_turns: int,
        tool_handler: Any | None = None,
    ) -> SessionResult:
        if session.thread_id is None:
            await self._ensure_thread_started(session)
        await session.request(
            {
                "id": "turn/start",
                "method": "turn/start",
                "params": {
                    "threadId": session.thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": prompt,
                            "text_elements": [],
                        }
                    ],
                    "approvalPolicy": self.config.approval_policy,
                },
            }
        )
        turns_completed = 0
        while turns_completed < max_turns:
            event = await self._next_event(session)
            event_type = event.type
            payload = event.payload
            if event_type == "session_started":
                session.session_id = payload.get("session_id") or payload.get("sessionId") or session.session_id
            elif event_type == "thread/started":
                thread = payload.get("thread") or {}
                session.thread_id = thread.get("id") or session.thread_id
            elif event_type == "thread/tokenUsage/updated":
                token_usage = payload.get("tokenUsage") or {}
                total = token_usage.get("total") or {}
                session.usage = UsageTotals.model_validate(
                    {
                        "prompt_tokens": total.get("inputTokens", 0),
                        "completion_tokens": total.get("outputTokens", 0),
                        "total_tokens": total.get("totalTokens", 0),
                        "runtime_seconds": session.usage.runtime_seconds,
                    }
                )
            elif event_type == "turn/completed":
                turns_completed += 1
                turn = payload.get("turn") or {}
                if turn.get("status") == "failed":
                    error = turn.get("error") or {}
                    raise AppServerError(error.get("message", "app_server_turn_failed"))
                break
            elif event_type == "item/tool/call":
                result = await self._handle_tool_call(payload, tool_handler)
                await session.request({"id": payload.get("id"), "result": result})
            elif event_type in {"item/tool/requestUserInput", "user_input_required"}:
                raise AppServerError("user_input_required")
            elif event_type == "error":
                error = payload.get("error") or {}
                raise AppServerError(error.get("message", "app_server_error"))
            elif event_type == "session.ended":
                break
        return SessionResult(
            session_id=session.session_id,
            turns_completed=turns_completed,
            usage=session.usage,
            rate_limits=session.rate_limits,
            normal_exit=True,
        )

    async def _ensure_thread_started(self, session: AppServerSession) -> None:
        while session.thread_id is None:
            event = await self._next_event(session)
            if event.type == "thread/started":
                thread = event.payload.get("thread") or {}
                session.thread_id = thread.get("id") or session.thread_id
            elif event.type == "message":
                result = event.payload.get("result", {})
                thread = result.get("thread", {})
                thread_id = thread.get("id")
                if thread_id:
                    session.thread_id = thread_id
            elif event.type == "stderr":
                continue
            elif event.type == "error":
                error = event.payload.get("error") or {}
                raise AppServerError(error.get("message", event.payload.get("message", "app_server_error")))
            elif event.type == "session.ended":
                raise AppServerError("thread_start_failed")

    async def _next_event(self, session: AppServerSession) -> SessionEvent:
        stdout = session.process.stdout
        stderr = session.process.stderr
        assert stdout is not None
        assert stderr is not None
        while True:
            tasks = {asyncio.create_task(stdout.readline())}
            stderr_task: asyncio.Task[bytes] | None = None
            if session.stderr_open:
                stderr_task = asyncio.create_task(stderr.readline())
                tasks.add(stderr_task)
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.config.turn_timeout_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if not done:
                raise AppServerError("turn_timeout")
            task = done.pop()
            data = await task
            if stderr_task is not None and task is stderr_task:
                if data:
                    return SessionEvent(type="stderr", payload={"line": data.decode().strip()})
                session.stderr_open = False
                continue
            if not data:
                if session.process.returncode not in (0, None):
                    raise AppServerError(f"app_server_exit:{session.process.returncode}")
                return SessionEvent(type="session.ended", payload={})
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise AppServerError("invalid_json_line") from exc
            method = payload.get("method")
            if method:
                return SessionEvent(type=method, payload=payload.get("params", {}))
            if payload.get("result", {}).get("sessionId"):
                return SessionEvent(type="session_started", payload={"sessionId": payload["result"]["sessionId"]})
            return SessionEvent(type="message", payload=payload)

    async def _handle_tool_call(self, payload: dict[str, Any], tool_handler: Any | None) -> dict[str, Any]:
        name = payload.get("name")
        if tool_handler is None:
            return {"success": False, "error": "unsupported_tool_call"}
        try:
            return await tool_handler(name, payload.get("input"))
        except Exception as exc:
            return {"success": False, "error": str(exc)}
