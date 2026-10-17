"""Fallback executor: a person carries the task to Antigravity (or any agent) and back.

Use it when the CLI is unavailable (for example after an update broke it). The
engine writes a paste-ready prompt and waits for a report file; evidence,
checks and review work exactly as with an automated executor, because they
never depend on the executor's claims.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from orch.executors.base import (
    Capabilities, Classification, EventCallback, ExecRequest, ExecResult, ExecStatus,
    ExecutionEngine, Health,
)
from orch.paths import resolve_paths
from orch.schema import REPORT_SCHEMA, validate

INSTRUCTIONS = """Task {task_id}, attempt {attempt}

1. Open Antigravity (or another coding agent) on: {workdir}
2. Paste the whole of prompt.md into a new conversation and let it work.
3. Save the agent's final JSON report as report.json in this folder:
   {folder}
   (schema: report.schema.json). Writing it by hand is fine.

orch is waiting for report.json, then it diffs the repository and runs the checks itself.
To give up, cancel the task: orch task cancel {task_id} --reason "..."
"""


class ManualExecutor(ExecutionEngine):
    name = "manual"

    def __init__(self, inbox: str | None = None, poll_seconds: float = 2.0):
        self.inbox = Path(inbox or os.environ.get("ORCH_MANUAL_DIR") or resolve_paths().data_dir / "manual")
        self.poll_seconds = poll_seconds

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_edit_files=True, can_run_commands=False, supports_resume=False, supports_cancel=True,
            supports_streaming=False, supports_structured_report=True, reports_usage=False,
            sends_code_off_machine=True,
        )

    def health(self) -> Health:
        return Health(ok=True, detail=f"hand-off folder {self.inbox}")

    def run(
        self,
        request: ExecRequest,
        cancel: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> ExecResult:
        started = time.monotonic()
        folder = self.inbox / f"{request.task_id}-a{request.attempt}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "prompt.md").write_text(request.prompt, encoding="utf-8")
        (folder / "report.schema.json").write_text(json.dumps(REPORT_SCHEMA, indent=2), encoding="utf-8")
        (folder / "README.txt").write_text(INSTRUCTIONS.format(
            task_id=request.task_id, attempt=request.attempt, workdir=request.workdir, folder=folder), encoding="utf-8")
        report_path = folder / "report.json"
        if on_event:
            on_event({"event": "waiting_for_human", "folder": str(folder), "prompt": str(folder / "prompt.md")})
        while not report_path.exists():
            if cancel is not None and cancel.is_set():
                return ExecResult(ExecStatus.CANCELLED, Classification.CANCELLED,
                                  duration_seconds=time.monotonic() - started)
            if time.monotonic() - started > request.timeout_seconds:
                return ExecResult(ExecStatus.TIMED_OUT, Classification.TIMEOUT, error=f"no report.json in {folder}",
                                  duration_seconds=time.monotonic() - started)
            time.sleep(self.poll_seconds)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return ExecResult(ExecStatus.FAILED, Classification.EXECUTOR_ERROR, error=f"report.json unreadable: {e}",
                              duration_seconds=time.monotonic() - started)
        errors = validate(report, REPORT_SCHEMA)
        return ExecResult(
            status=ExecStatus.SUCCEEDED,
            report=report if not errors else None,
            error=("report.json does not match the schema: " + "; ".join(errors[:5])) if errors else None,
            duration_seconds=round(time.monotonic() - started, 2),
        )
