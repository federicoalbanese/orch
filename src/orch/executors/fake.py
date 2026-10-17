"""A scripted executor for tests and dry runs. It never leaves the machine."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch.executors.base import (
    Capabilities, Classification, EventCallback, ExecRequest, ExecResult, ExecStatus,
    ExecutionEngine, Health, Mode,
)


@dataclass
class FakeStep:
    """One scripted executor turn. files maps relative path -> new content (None deletes)."""

    files: dict[str, str | None] = field(default_factory=dict)
    status: ExecStatus = ExecStatus.SUCCEEDED
    classification: Classification = Classification.NONE
    report: dict[str, Any] | None = None
    response: str = ""
    usage: dict[str, int] | None = None
    denied_actions: list[str] = field(default_factory=list)
    error: str | None = None
    delay: float = 0.0


class FakeExecutor(ExecutionEngine):
    name = "fake"

    def __init__(self, steps: list[FakeStep] | dict[str, list[FakeStep]] | None = None):
        self._steps = steps if steps is not None else []
        self._lock = threading.Lock()
        self._conversations = 0
        self.requests: list[ExecRequest] = []

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_edit_files=True, can_run_commands=False, supports_resume=True, supports_cancel=True,
            supports_streaming=True, supports_structured_report=True, reports_usage=True,
            sends_code_off_machine=False,
        )

    def health(self) -> Health:
        return Health(ok=True, detail="scripted executor", version="fake")

    def _next_step(self, task_id: str) -> FakeStep:
        with self._lock:
            queue = self._steps.get(task_id, []) if isinstance(self._steps, dict) else self._steps
            if not queue:
                return FakeStep(report={"status": "DONE", "files_changed": [], "summary": "no-op", "unresolved": []})
            return queue.pop(0)

    def run(
        self,
        request: ExecRequest,
        cancel: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> ExecResult:
        started = time.monotonic()
        self.requests.append(request)
        with self._lock:
            if request.resume_conversation_id:
                conversation = request.resume_conversation_id
            else:
                self._conversations += 1
                conversation = f"fake-{self._conversations}"
        step = self._next_step(request.task_id)
        emit = on_event or (lambda e: None)
        emit({"event": "init", "conversation_id": conversation})

        deadline = started + request.timeout_seconds
        while time.monotonic() - started < step.delay:
            if cancel is not None and cancel.is_set():
                return ExecResult(ExecStatus.CANCELLED, Classification.CANCELLED, conversation,
                                  duration_seconds=time.monotonic() - started)
            if time.monotonic() > deadline:
                return ExecResult(ExecStatus.TIMED_OUT, Classification.TIMEOUT, conversation,
                                  duration_seconds=time.monotonic() - started)
            time.sleep(0.01)

        if step.files and request.mode == Mode.READ_ONLY:
            return ExecResult(ExecStatus.DENIED, Classification.PERMISSION, conversation,
                              denied_actions=["write_file"], error="read-only request tried to edit files",
                              duration_seconds=time.monotonic() - started)
        root = Path(request.workdir).resolve()
        for rel, content in step.files.items():
            target = (root / rel).resolve()
            if not target.is_relative_to(root):
                return ExecResult(ExecStatus.FAILED, Classification.EXECUTOR_ERROR, conversation,
                                  error=f"refusing to write outside the workdir: {rel}",
                                  duration_seconds=time.monotonic() - started)
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            emit({"event": "step_update", "file": rel})

        result = ExecResult(
            status=step.status,
            classification=step.classification,
            conversation_id=conversation,
            report=step.report,
            response_text=step.response,
            usage=step.usage,
            denied_actions=list(step.denied_actions),
            error=step.error,
            duration_seconds=time.monotonic() - started,
        )
        emit({"event": "result", "status": result.status.value})
        return result
