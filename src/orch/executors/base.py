"""The execution-engine contract.

An executor turns one ExecRequest into one ExecResult. It never decides what
counts as success: the engine collects evidence (diff, scope check,
verification runs) independently and Claude reviews it. The executor's own
report is just one more piece of evidence.

run() blocks. Cancellation is cooperative through `cancel` (a threading.Event
the adapter must poll), and progress is reported through `on_event`.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecStatus(str, Enum):
    SUCCEEDED = "succeeded"        # the executor finished its turn (says nothing about correctness)
    FAILED = "failed"              # the executor errored
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DENIED = "denied"              # the executor needed a permission it does not have
    ENV_BLOCKED = "env_blocked"    # account, licence, location, eligibility, missing binary


class Classification(str, Enum):
    """How the engine should react to a non-success."""

    NONE = "none"
    TRANSIENT = "transient"          # retry with backoff (rate limit, network)
    ENV_BLOCKED = "env_blocked"      # stop; a human must fix the environment
    PERMISSION = "permission"        # stop or rephrase; the executor lacks a capability
    EXECUTOR_ERROR = "executor_error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class Mode(str, Enum):
    READ_ONLY = "read_only"
    EDIT = "edit"


@dataclass(frozen=True)
class Capabilities:
    can_edit_files: bool
    can_run_commands: bool
    supports_resume: bool
    supports_cancel: bool
    supports_streaming: bool
    supports_structured_report: bool
    reports_usage: bool
    sends_code_off_machine: bool
    # True when reported usage is a running total for the whole conversation, not for this run.
    cumulative_usage: bool = False


@dataclass
class Health:
    ok: bool
    detail: str
    version: str | None = None
    # The version the adapter was verified against, when it depends on a tool that updates itself.
    verified_version: str | None = None


@dataclass
class ExecRequest:
    task_id: str
    attempt: int
    workdir: str
    prompt: str
    mode: Mode = Mode.EDIT
    report_schema: dict[str, Any] | None = None
    model: str | None = None
    timeout_seconds: int = 1800
    resume_conversation_id: str | None = None


@dataclass
class ExecResult:
    status: ExecStatus
    classification: Classification = Classification.NONE
    conversation_id: str | None = None
    report: dict[str, Any] | None = None
    response_text: str = ""
    usage: dict[str, int] | None = None
    denied_actions: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    transcript_path: str | None = None


EventCallback = Callable[[dict[str, Any]], None]


class ExecutionEngine(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def health(self) -> Health: ...

    @abstractmethod
    def run(
        self,
        request: ExecRequest,
        cancel: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> ExecResult: ...
