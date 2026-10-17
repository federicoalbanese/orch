"""Execution engines. New engines register here without touching the core."""

from __future__ import annotations

from collections.abc import Callable

from orch.errors import OrchError
from orch.executors.agy import AgyExecutor
from orch.executors.base import ExecutionEngine
from orch.executors.fake import FakeExecutor
from orch.executors.manual import ManualExecutor

_FACTORIES: dict[str, Callable[[], ExecutionEngine]] = {
    "agy": AgyExecutor,
    "manual": ManualExecutor,
    "fake": FakeExecutor,
}


def register(name: str, factory: Callable[[], ExecutionEngine]) -> None:
    _FACTORIES[name] = factory


def available() -> list[str]:
    return sorted(_FACTORIES)


def get_executor(name: str) -> ExecutionEngine:
    if name in _FACTORIES:
        return _FACTORIES[name]()
    raise OrchError(f"unknown executor {name!r}; available: {', '.join(available())}")
