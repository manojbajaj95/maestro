"""Service factory helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .app_server import CodexAppServerClient
from .logging import configure_logging
from .orchestrator import SymphonyOrchestrator
from .runner import AgentRunner
from .server import build_app
from .tracker import build_tracker_client
from .workspace import WorkspaceManager


@dataclass(slots=True)
class ServiceBundle:
    orchestrator: SymphonyOrchestrator
    app: Any


def build_service(config) -> ServiceBundle:
    logger = configure_logging()
    tracker = build_tracker_client(config.tracker)
    workspace_manager = WorkspaceManager(config)
    app_server = CodexAppServerClient(config.codex)
    runner = AgentRunner(config, tracker, workspace_manager, app_server)
    orchestrator = SymphonyOrchestrator(config, tracker, workspace_manager, runner, logger)
    app = build_app(orchestrator)
    return ServiceBundle(orchestrator=orchestrator, app=app)
