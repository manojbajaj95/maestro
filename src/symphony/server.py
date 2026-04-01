"""Optional HTTP observability server."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .orchestrator import SymphonyOrchestrator


def build_app(orchestrator: SymphonyOrchestrator) -> FastAPI:
    app = FastAPI(title="Symphony")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/state")
    async def state() -> dict[str, object]:
        return orchestrator.state.snapshot().model_dump(mode="json")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        snapshot = orchestrator.state.snapshot()
        running = (
            "".join(
                f"<li>{entry.identifier} ({entry.state_name}) attempt {entry.attempt}</li>"
                for entry in snapshot.running.values()
            )
            or "<li>No active runs</li>"
        )
        retries = (
            "".join(
                f"<li>{entry.identifier} retry {entry.attempt} due at {entry.due_at_ms}</li>"
                for entry in snapshot.retry_attempts.values()
            )
            or "<li>No scheduled retries</li>"
        )
        return f"""
        <html>
          <head><title>Symphony Status</title></head>
          <body>
            <h1>Symphony</h1>
            <p>Poll interval: {snapshot.poll_interval_ms} ms</p>
            <p>Max concurrent agents: {snapshot.max_concurrent_agents}</p>
            <h2>Running</h2>
            <ul>{running}</ul>
            <h2>Retries</h2>
            <ul>{retries}</ul>
            <h2>Totals</h2>
            <p>Total tokens: {snapshot.codex_totals.total_tokens}</p>
            <p>Runtime seconds: {snapshot.codex_totals.runtime_seconds}</p>
          </body>
        </html>
        """

    return app
