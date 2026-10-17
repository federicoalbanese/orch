"""Antigravity CLI (`agy`) adapter, built on behaviour verified on this machine.

Verified (docs/architecture.md, section 2):
- `agy -p <prompt> --output-format stream-json` streams NDJSON: one `init`,
  many `step_update` (tool calls, text deltas, finish), one `result`.
- `--json-schema` yields `result.structured_output`, validated by agy.
- `--mode accept-edits` lets it edit files in its working directory.
- `--conversation <id>` resumes a conversation (used for corrections).
- Headless runs cannot execute shell commands; attempts come back in
  `result.denied_actions`. The engine runs verification itself.
- On `--print-timeout`, agy prints "print timeout ..." on stderr and still reports
  status SUCCESS with an empty response, so stderr decides, not the status field.
- SIGTERM ends a run cleanly (exit 1, "interrupted").

The stream is parsed defensively: unreadable lines and events of the wrong shape
are skipped and counted, so a corrupted or truncated stream ends as a classified
executor error, never as an exception.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from orch.errors import ValidationError
from orch.evidence import normalize_claimed
from orch.executors.base import (
    Capabilities, Classification, EventCallback, ExecRequest, ExecResult, ExecStatus,
    ExecutionEngine, Health, Mode,
)
from orch.schema import REPORT_SCHEMA, validate

# Linux limits a single argv string to 128 KiB (MAX_ARG_STRLEN); agy takes the prompt as an argument.
MAX_PROMPT_BYTES = 120_000
KILL_GRACE_SECONDS = 10.0
DEADLINE_SLACK_SECONDS = 60.0
# The agy version every behaviour above was verified against (docs/architecture.md, R1).
VERIFIED_VERSION = "1.4.0"
PROBE_PROMPT = ("This is an account check by orch. Do not use any tools and do not read or change any files. "
                "Reply with the single word OK.")

_ENV_BLOCKED = [
    (re.compile(r"user location is not supported", re.I), "Google rejected the network location (HTTP 400)"),
    (re.compile(r"SUBSCRIPTION_REQUIRED|#3501|valid license", re.I), "the Google account has no licence (HTTP 403)"),
    (re.compile(r"eligibility check failed|not eligible", re.I),
     "agy reports that this account or region is not eligible for Antigravity"),
    (re.compile(r"not (?:logged|signed) in|unauthenticated|login required", re.I), "agy is not signed in"),
]
_TRANSIENT = [
    re.compile(r"\b429\b|RESOURCE_EXHAUSTED|too many requests", re.I),
    re.compile(r"\b50[023]\b|internal error encountered|UNAVAILABLE|deadline exceeded", re.I),
    re.compile(r"connection (?:reset|refused)|network is unreachable|temporary failure in name resolution|i/o timeout", re.I),
]


def classify(
    returncode: int | None,
    stderr: str,
    result: dict[str, Any] | None,
    cancelled: bool,
    hard_timeout: bool,
) -> tuple[ExecStatus, Classification, str | None]:
    if cancelled:
        return ExecStatus.CANCELLED, Classification.CANCELLED, "cancelled"
    if hard_timeout or re.search(r"print timeout", stderr, re.I):
        return ExecStatus.TIMED_OUT, Classification.TIMEOUT, "agy hit its time limit before finishing the turn"
    # Only look at diagnostics, never at the model's own words (a task may well be about "eligibility").
    meta = {k: v for k, v in (result or {}).items() if k not in ("response", "structured_output", "json_schema")}
    text = stderr + "\n" + json.dumps(meta)
    for pattern, message in _ENV_BLOCKED:
        if pattern.search(text):
            return ExecStatus.ENV_BLOCKED, Classification.ENV_BLOCKED, message
    for pattern in _TRANSIENT:
        if pattern.search(text):
            return ExecStatus.FAILED, Classification.TRANSIENT, _tail(stderr) or "transient service error"
    if result is None:
        return ExecStatus.FAILED, Classification.EXECUTOR_ERROR, _tail(stderr) or f"agy exited {returncode} without a result"
    if result.get("denied_actions") and not (result.get("response") or "").strip() and not result.get("structured_output"):
        names = ", ".join(_denied_names(result))
        return ExecStatus.DENIED, Classification.PERMISSION, f"agy needed permissions headless mode denies: {names}"
    if result.get("status") != "SUCCESS":
        return ExecStatus.FAILED, Classification.EXECUTOR_ERROR, f"agy status {result.get('status')!r}: {_tail(stderr)}"
    return ExecStatus.SUCCEEDED, Classification.NONE, None


def _text(value: Any) -> str:
    """A string field from the stream; anything else (a number, an object, null) becomes text or ''."""
    if isinstance(value, str):
        return value
    return "" if value is None else json.dumps(value)


def _sanitize_result(raw: Any) -> dict[str, Any] | None:
    """The result object with every field the adapter reads coerced to the type it expects."""
    if not isinstance(raw, dict):
        return None
    result = dict(raw)
    for key in ("status", "response", "conversation_id"):
        if key in result:
            result[key] = _text(result[key])
    denied = result.get("denied_actions")
    if isinstance(denied, str):
        result["denied_actions"] = [denied]
    elif not isinstance(denied, list):
        result["denied_actions"] = []
    if not isinstance(result.get("usage"), dict):
        result.pop("usage", None)
    return result


def _tail(text: str, n: int = 600) -> str:
    text = text.strip()
    return text[-n:]


def _denied_names(result: dict[str, Any]) -> list[str]:
    return [_text(d.get("display_name") or d.get("action")) or json.dumps(d) if isinstance(d, dict) else _text(d)
            for d in result.get("denied_actions") or []]


def _usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    return {k: int(v) for k, v in raw.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def extract_report(result: dict[str, Any] | None, workdir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The executor's structured report, normalised, or (None, why it is unusable)."""
    if not result:
        return None, "no result"
    report = result.get("structured_output")
    if report is None:
        text = (result.get("response") or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
        candidate = fenced.group(1) if fenced else text
        try:
            report = json.loads(candidate) if candidate.startswith("{") else None
        except json.JSONDecodeError:
            report = None
    if not isinstance(report, dict):
        return None, "the executor returned no structured report"
    errors = validate(report, REPORT_SCHEMA)
    if errors:
        return None, "the executor's report does not match the schema: " + "; ".join(errors[:5])
    report = dict(report)
    report["files_changed"] = sorted({normalize_claimed(p, workdir) for p in report.get("files_changed", [])})
    return report, None


class AgyExecutor(ExecutionEngine):
    name = "agy"

    def __init__(self, binary: str | None = None, brain_dir: str | None = None):
        self.binary = binary or os.environ.get("ORCH_AGY_BIN") or shutil.which("agy")
        self.brain_dir = Path(
            brain_dir or os.environ.get("ORCH_AGY_BRAIN_DIR") or "~/.gemini/antigravity-cli/brain"
        ).expanduser()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_edit_files=True, can_run_commands=False, supports_resume=True, supports_cancel=True,
            supports_streaming=True, supports_structured_report=True, reports_usage=True,
            sends_code_off_machine=True,
            # Verified: three resumed turns reported 31, 62, 93 output tokens.
            cumulative_usage=True,
        )

    def health(self) -> Health:
        if not self.binary or not Path(self.binary).exists():
            return Health(ok=False, detail="agy not found (install the Antigravity CLI or set ORCH_AGY_BIN)")
        try:
            proc = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            return Health(ok=False, detail=f"agy --version failed: {e}")
        version = proc.stdout.strip() or None
        return Health(ok=proc.returncode == 0, detail=f"{self.binary} {version or ''}".strip(), version=version,
                      verified_version=VERIFIED_VERSION)

    def probe(self, model: str | None = None, timeout_seconds: int = 120) -> ExecResult:
        """One tiny read-only turn, to check that the account works (sign-in, licence, location, eligibility, quota).

        It runs in an empty temporary directory, so no repository content is involved, but the
        prompt still goes to Google and spends a little quota. Callers run it only when asked.
        """
        with tempfile.TemporaryDirectory(prefix="orch-agy-probe-") as tmp:
            return self.run(ExecRequest(task_id="doctor-probe", attempt=1, workdir=tmp, prompt=PROBE_PROMPT,
                                        mode=Mode.READ_ONLY, model=model, timeout_seconds=timeout_seconds))

    def build_argv(self, req: ExecRequest) -> list[str]:
        if not self.binary:
            raise ValidationError("agy binary not found")
        if len(req.prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValidationError(
                f"prompt is {len(req.prompt.encode())} bytes; agy accepts at most {MAX_PROMPT_BYTES} "
                "(lower limits.context_budget_chars)"
            )
        argv = [self.binary, "-p", req.prompt, "--output-format", "stream-json",
                "--print-timeout", f"{max(1, int(req.timeout_seconds))}s"]
        if req.mode == Mode.EDIT:
            argv += ["--mode", "accept-edits"]
        if req.model:
            argv += ["--model", req.model]
        if req.resume_conversation_id:
            argv += ["--conversation", req.resume_conversation_id]
        if req.report_schema:
            schema = {k: v for k, v in req.report_schema.items() if k != "$id"}
            argv += ["--json-schema", json.dumps(schema)]
        return argv

    def run(
        self,
        request: ExecRequest,
        cancel: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> ExecResult:
        emit = on_event or (lambda e: None)
        started = time.monotonic()
        workdir = Path(request.workdir)
        try:
            argv = self.build_argv(request)
        except ValidationError as e:
            return ExecResult(ExecStatus.FAILED, Classification.EXECUTOR_ERROR, error=str(e))
        try:
            proc = subprocess.Popen(
                argv, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
        except OSError as e:
            return ExecResult(ExecStatus.ENV_BLOCKED, Classification.ENV_BLOCKED, error=f"cannot start agy: {e}")
        emit({"event": "process", "pid": proc.pid})

        lines: queue.Queue[str | None] = queue.Queue()
        stderr_chunks: list[str] = []

        def pump_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def pump_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                if sum(len(c) for c in stderr_chunks) < 200_000:
                    stderr_chunks.append(line)

        threads = [threading.Thread(target=pump_stdout, daemon=True), threading.Thread(target=pump_stderr, daemon=True)]
        for t in threads:
            t.start()

        conversation = request.resume_conversation_id
        result: dict[str, Any] | None = None
        unreadable = 0
        cancelled = hard_timeout = False
        deadline = started + request.timeout_seconds + DEADLINE_SLACK_SECONDS
        stdout_done = False
        while not stdout_done:
            if cancel is not None and cancel.is_set() and not cancelled:
                cancelled = True
                self._terminate(proc)
            if time.monotonic() > deadline and not hard_timeout and not cancelled:
                hard_timeout = True
                self._terminate(proc)
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                stdout_done = True
                continue
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                unreadable += 1
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            if not isinstance(event, dict):
                unreadable += 1
                continue
            kind = event.get("event")
            if kind == "init":
                conversation = _text(event.get("conversation_id")) or conversation
                emit({"event": "init", "conversation_id": conversation})
            elif kind == "step_update":
                step = event.get("step_update")
                if not isinstance(step, dict):
                    unreadable += 1
                elif step.get("step_type") == "tool":
                    emit({"event": "tool", "name": _text(step.get("tool_name")), "state": _text(step.get("state"))})
                elif step.get("step_type") == "finish":
                    emit({"event": "finish"})
            elif kind == "result":
                result = _sanitize_result(event.get("result"))
                if result is None:
                    unreadable += 1
                else:
                    conversation = result.get("conversation_id") or conversation
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._terminate(proc)
        for t in threads:
            t.join(timeout=5)

        stderr = "".join(stderr_chunks)
        status, classification, error = classify(proc.returncode, stderr, result, cancelled, hard_timeout)
        if result is None and unreadable and classification == Classification.EXECUTOR_ERROR:
            error = f"{error} ({unreadable} unreadable stream line(s); the output was corrupted or cut off)"
        report, report_problem = extract_report(result, workdir)
        if status == ExecStatus.SUCCEEDED and report_problem and request.report_schema:
            error = report_problem
        transcript = None
        if conversation and re.fullmatch(r"[\w-]+", conversation):
            path = self.brain_dir / conversation / ".system_generated" / "logs" / "transcript.jsonl"
            transcript = str(path) if path.exists() else None
        result_out = ExecResult(
            status=status,
            classification=classification,
            conversation_id=conversation,
            report=report,
            response_text=(result or {}).get("response") or "",
            usage=_usage((result or {}).get("usage")),
            denied_actions=_denied_names(result or {}),
            error=error,
            duration_seconds=round(time.monotonic() - started, 2),
            transcript_path=transcript,
        )
        emit({"event": "result", "status": status.value, "classification": classification.value})
        return result_out

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
