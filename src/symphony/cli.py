"""CLI entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from .service import build_service
from .workflow import build_service_config, load_workflow, resolve_runtime_path


async def _run(args: argparse.Namespace) -> int:
    workflow_path = resolve_runtime_path(args.workflow_path)
    workflow = load_workflow(workflow_path)
    config = build_service_config(workflow)
    if args.port is not None:
        config.server.port = args.port
    bundle = build_service(config)
    if config.server.port is not None:
        server = uvicorn.Server(
            uvicorn.Config(bundle.app, host=config.server.host, port=config.server.port, log_level="info")
        )
        orchestrator_task = asyncio.create_task(bundle.orchestrator.run_forever())
        server_task = asyncio.create_task(server.serve())
        done, pending = await asyncio.wait(
            {orchestrator_task, server_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
        return 0
    await bundle.orchestrator.run_forever()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Symphony orchestrator")
    parser.add_argument("workflow_path", nargs="?", help="Path to WORKFLOW.md")
    parser.add_argument("--port", type=int, default=None, help="Override HTTP server port")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
