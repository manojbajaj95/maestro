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
        await session.request({"id": "turn/start", "method": "turn/start", "params": {}})
        return session

    async def run_turns(
        self,
        session: AppServerSession,
        prompt: str,
        max_turns: int,
        tool_handler: Any | None = None,
    ) -> SessionResult:
        await session.request(
            {
                "id": "item/create",
                "method": "item/create",
                "params": {"item": {"type": "message", "role": "user", "content": [{"type": "text", "text": prompt}]}}
            }
        )
        await session.request({"id": "response/create", "method": "response/create", "params": {}})
        turns_completed = 0
        while turns_completed < max_turns:
            event = await self._next_event(session)
            event_type = event.type
            payload = event.payload
            if event_type == "session_started":
                session.session_id = payload.get("session_id") or payload.get("sessionId") or session.session_id
            elif event_type == "rate_limits":
                session.rate_limits = RateLimitSnapshot.model_validate(payload)
            elif event_type == "usage":
                session.usage = UsageTotals.model_validate(
                    {
                        "prompt_tokens": payload.get("prompt_tokens", 0),
                        "completion_tokens": payload.get("completion_tokens", 0),
                        "total_tokens": payload.get("total_tokens", 0),
                        "runtime_seconds": payload.get("runtime_seconds", 0.0),
                    }
                )
            elif event_type == "turn.completed":
                turns_completed += 1
                if turns_completed >= max_turns:
                    break
                await session.request({"id": "response/create", "method": "response/create", "params": {}})
            elif event_type == "item/tool/call":
                result = await self._handle_tool_call(payload, tool_handler)
                await session.request({"id": payload.get("id"), "result": result})
            elif event_type in {"item/tool/requestUserInput", "user_input_required"}:
                raise AppServerError("user_input_required")
            elif event_type == "error":
                raise AppServerError(payload.get("message", "app_server_error"))
            elif event_type == "session.ended":
                break
        return SessionResult(
            session_id=session.session_id,
            turns_completed=turns_completed,
            usage=session.usage,
            rate_limits=session.rate_limits,
            normal_exit=True,
        )

    async def _next_event(self, session: AppServerSession) -> SessionEvent:
        stdout = session.process.stdout
        stderr = session.process.stderr
        assert stdout is not None
        assert stderr is not None
        line_task = asyncio.create_task(stdout.readline())
        err_task = asyncio.create_task(stderr.readline())
        done, pending = await asyncio.wait(
            {line_task, err_task},
            timeout=self.config.turn_timeout_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if not done:
            raise AppServerError("turn_timeout")
        task = done.pop()
        data = await task
        if task is err_task:
            if data:
                return SessionEvent(type="stderr", payload={"line": data.decode().strip()})
            raise AppServerError("app_server_stderr_closed")
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
