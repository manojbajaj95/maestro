from __future__ import annotations

import asyncio
import json
from asyncio.subprocess import Process
from typing import cast

import pytest

from symphony.app_server import AppServerSession, CodexAppServerClient
from symphony.models import CodexConfig


class FakeStream:
    def __init__(self, items: list[tuple[float, bytes]]) -> None:
        self.items = items

    async def readline(self) -> bytes:
        if not self.items:
            return b""
        delay, payload = self.items[0]
        if delay:
            await asyncio.sleep(delay)
        self.items.pop(0)
        return payload


class FakeProcess:
    def __init__(self, stdout: FakeStream, stderr: FakeStream, returncode: int | None = None) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = None
        self.returncode = returncode


@pytest.mark.asyncio
async def test_next_event_ignores_closed_stderr_if_stdout_is_active() -> None:
    stdout_payload = json.dumps({"method": "turn.completed", "params": {}}).encode() + b"\n"
    process = FakeProcess(
        stdout=FakeStream([(0.01, stdout_payload)]),
        stderr=FakeStream([(0, b"")]),
    )
    session = AppServerSession(cast(Process, process), CodexConfig())
    client = CodexAppServerClient(CodexConfig())

    event = await client._next_event(session)

    assert event.type == "turn.completed"
    assert session.stderr_open is False


@pytest.mark.asyncio
async def test_next_event_surfaces_stderr_lines_without_failing() -> None:
    process = FakeProcess(
        stdout=FakeStream([(0.01, b"")]),
        stderr=FakeStream([(0, b"warning on stderr\n")]),
        returncode=0,
    )
    session = AppServerSession(cast(Process, process), CodexConfig())
    client = CodexAppServerClient(CodexConfig())

    event = await client._next_event(session)

    assert event.type == "stderr"
    assert event.payload["line"] == "warning on stderr"
