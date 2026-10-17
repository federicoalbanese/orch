"""Exception hierarchy. Each error carries the CLI exit code it maps to."""

from __future__ import annotations


class OrchError(Exception):
    exit_code = 1


class ValidationError(OrchError):
    """Input (a plan, a project setting, a report) failed validation."""

    exit_code = 3

    def __init__(self, message: str, errors: list[str] | None = None):
        self.errors = list(errors or [])
        detail = "".join(f"\n  - {e}" for e in self.errors)
        super().__init__(f"{message}{detail}")


class ConfigError(ValidationError):
    """The configuration file is invalid."""


class NotFoundError(OrchError):
    exit_code = 4


class PolicyError(OrchError):
    """An action is not allowed, or needs an approval it does not have."""

    exit_code = 5


class TransitionError(OrchError):
    """A task state change is illegal, or another writer changed the task first."""

    exit_code = 6
