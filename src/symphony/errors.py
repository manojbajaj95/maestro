"""Project error types."""

from __future__ import annotations


class SymphonyError(Exception):
    """Base class for typed Symphony failures."""


class WorkflowError(SymphonyError):
    """Raised when workflow loading or validation fails."""


class ConfigError(SymphonyError):
    """Raised when effective config cannot be built."""


class TrackerError(SymphonyError):
    """Raised when tracker calls fail."""


class WorkspaceError(SymphonyError):
    """Raised when workspace preparation or cleanup fails."""


class AppServerError(SymphonyError):
    """Raised when the app-server client encounters a fatal issue."""
