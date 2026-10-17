"""Dispatch, evidence, automatic fixes, retries, cancellation and recovery.

One dispatch takes a READY task through:
  RUNNING -> EXECUTED -> VERIFYING -> (auto-fix: READY -> RUNNING ...) -> IN_REVIEW
or ends it in FAILED / CANCELLED with the reason recorded. Review decisions are
made separately (orch.review), never by the engine.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orch import context, evidence, executors, policy, prompts
from orch.config import Config
from orch.errors import OrchError, PolicyError, TransitionError
from orch.executors.base import Classification, ExecRequest, ExecResult, ExecStatus, ExecutionEngine, Mode
from orch.models import ReviewDecision, Task, TaskStatus
from orch.paths import Paths
from orch.redact import redact, truncate_middle
from orch.registry import effective_config
from orch.schema import REPORT_SCHEMA
from orch.statemachine import ACTIVE
from orch.store import Store
from orch.util import now

ProgressCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class Outcome:
    task_id: str
    status: str
    message: str
    runs: list[int] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checks"] = [{k: c.get(k) for k in ("name", "passed", "exit_code", "timed_out", "skipped")} for c in self.checks]
        return d


def usage_delta(previous: dict[str, int] | None, current: dict[str, int] | None) -> dict[str, int] | None:
    """This run's own usage when the executor reports running totals per conversation."""
    if current is None:
        return None
    if not previous:
        return dict(current)
    return {k: max(0, int(v) - int(previous.get(k, 0))) for k, v in current.items()}


class Engine:
    def __init__(
        self,
        store: Store,
        paths: Paths,
        cfg: Config,
        executor_factory: Callable[[str], ExecutionEngine] = executors.get_executor,
        progress: ProgressCallback | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cancel_poll_seconds: float = 1.0,
        executor_override: str | None = None,
    ):
        self.executor_override = executor_override
        self.store = store
        self.paths = paths
        self.cfg = cfg
        self.executor_factory = executor_factory
        self.progress = progress or (lambda tid, e: None)
        self.sleep = sleep
        self.cancel_poll_seconds = cancel_poll_seconds

    # -- helpers ----------------------------------------------------------

    def _last_conversation(self, task_id: str) -> str | None:
        for run in reversed(self.store.list_runs(task_id)):
            if run.get("conversation_id"):
                return run["conversation_id"]
        return None

    def _conversation_usage(self, task_id: str, conversation: str, current_run: int) -> dict[str, int]:
        """Running total already recorded for a conversation (the sum of its earlier runs' increments)."""
        total: dict[str, int] = {}
        for run in self.store.list_runs(task_id):
            if run["id"] != current_run and run.get("conversation_id") == conversation:
                for k, v in (run.get("usage") or {}).items():
                    total[k] = total.get(k, 0) + int(v)
        return total

    def _watch_cancel(self, task_id: str, cancel: threading.Event, stop: threading.Event) -> threading.Thread:
        db = self.store.path

        def loop() -> None:
            s = Store(db)
            try:
                while not stop.wait(self.cancel_poll_seconds):
                    if s.get_task(task_id).status == TaskStatus.CANCELLED:
                        cancel.set()
                        return
            finally:
                s.close()

        t = threading.Thread(target=loop, daemon=True, name=f"cancel-watch-{task_id}")
        t.start()
        return t

    def _bounce(self, task_id: str, reason: str) -> None:
        """RUNNING -> INTERRUPTED -> READY, for a retry within the same dispatch."""
        self.store.transition_task(task_id, TaskStatus.INTERRUPTED, "engine", reason, expected=TaskStatus.RUNNING)
        self.store.transition_task(task_id, TaskStatus.READY, "engine", "retrying", expected=TaskStatus.INTERRUPTED)

    def _executor_evidence(self, result: ExecResult, limit: int) -> dict[str, Any]:
        text, truncated = truncate_middle(redact(result.response_text or ""), limit)
        return {
            "status": result.status.value, "classification": result.classification.value,
            "error": result.error, "denied_actions": result.denied_actions, "conversation_id": result.conversation_id,
            "usage_reported": result.usage, "duration_seconds": result.duration_seconds,
            "transcript_path": result.transcript_path, "response": text, "response_truncated": truncated,
        }

    # -- dispatch ---------------------------------------------------------

    def preflight(self, task: Task, include_git: bool = True) -> tuple[list[str], str]:
        """Everything that stops this task from starting now (empty list = it may start)."""
        project = self.store.get_project(task.project_id)
        pcfg = effective_config(self.cfg, project)
        executor_name = self.executor_override or project.executor or pcfg.get("executor", "default")
        try:
            external = self.executor_factory(executor_name).capabilities().sends_code_off_machine
        except OrchError as e:
            return [str(e)], executor_name
        problems = [str(b) for b in policy.dispatch_blockers(task.spec, project, executor_name, external)]
        if task.status != TaskStatus.READY:
            problems.append(f"task is {task.status.value}; only READY tasks can be dispatched")
        for dep in task.depends_on:
            if self.store.get_task(dep).status != TaskStatus.APPROVED:
                problems.append(f"dependency {dep} is not APPROVED yet")
        if include_git and not problems:
            problems += evidence.preflight(Path(project.root), project, task.spec, pcfg,
                                           self._last_after_tree(task.id), self.store.approved_changes(project.id))
        return problems, executor_name

    def _last_after_tree(self, task_id: str) -> str | None:
        for run in reversed(self.store.list_runs(task_id)):
            if run.get("after_tree"):
                return run["after_tree"]
        return None

    def dispatch(self, task_id: str) -> Outcome:
        task = self.store.get_task(task_id)
        project = self.store.get_project(task.project_id)
        pcfg = effective_config(self.cfg, project)
        root = Path(project.root)
        new_branch = None
        problems, executor_name = self.preflight(task, include_git=False)
        if not problems:
            try:
                new_branch = evidence.ensure_working_branch(root, f"orch/P{task.plan_id}", pcfg)
            except PolicyError as e:
                problems.append(str(e))
            if new_branch:
                self.store.add_event("git.branch_created", "task", task_id, "engine", project.id, {"branch": new_branch})
            problems += evidence.preflight(root, project, task.spec, pcfg, self._last_after_tree(task_id),
                                           self.store.approved_changes(project.id))
        if problems:
            raise PolicyError(f"cannot dispatch {task_id}:\n" + "\n".join(f"  - {p}" for p in problems))

        executor = self.executor_factory(executor_name)
        caps = executor.capabilities()
        limits = lambda key: pcfg.get("limits", key)  # noqa: E731
        model = task.spec.model or project.model or pcfg.get("executor", "model")
        checks = evidence.check_names(task.spec)
        extras = self.store.task_extras(task_id)

        if task.review_status == ReviewDecision.NEEDS_CORRECTION:
            kind = "correction"
            prompt = prompts.correction(task, self.store.list_reviews(task_id), caps, checks)
        else:
            kind = "execute"
            bundle = context.build_context(self.store, self.paths, project, limits("context_budget_chars"), task)
            prompt = prompts.initial(bundle.render(), task, caps, checks)
        conversation = self._last_conversation(task_id) if kind != "execute" and caps.supports_resume else None

        outcome = Outcome(task_id, "", "")
        auto_fixes, transient_used, denied_retried = 0, 0, False
        while True:
            task = self.store.get_task(task_id)
            attempt = task.attempt + 1
            before = evidence.snapshot(root)
            baseline = extras["baseline_tree"] or before.tree
            updates: dict[str, Any] = {"attempt": attempt}
            if not extras["baseline_tree"]:
                updates["baseline_tree"] = baseline
                extras["baseline_tree"] = baseline
            self.store.transition_task(
                task_id, TaskStatus.RUNNING, "engine", f"dispatch {kind} attempt {attempt} to {executor_name}",
                expected=TaskStatus.READY, updates=updates,
            )
            run_id = self.store.insert_run(task_id, attempt, kind, executor_name, os.getpid(), conversation)
            self.store.update_run(run_id, before_tree=before.tree)
            outcome.runs.append(run_id)
            self.progress(task_id, {"event": "dispatched", "run": run_id, "kind": kind, "attempt": attempt})

            def on_event(e: dict[str, Any], run_id: int = run_id) -> None:
                if e.get("event") == "process":
                    self.store.update_run(run_id, pid=e["pid"])
                self.progress(task_id, e)

            cancel, stop = threading.Event(), threading.Event()
            watcher = self._watch_cancel(task_id, cancel, stop)
            request = ExecRequest(
                task_id=task_id, attempt=attempt, workdir=str(root), prompt=prompt, mode=Mode.EDIT,
                report_schema=REPORT_SCHEMA if caps.supports_structured_report else None, model=model,
                timeout_seconds=limits("run_timeout_seconds"), resume_conversation_id=conversation,
            )
            try:
                result = executor.run(request, cancel, on_event)
            finally:
                stop.set()
                watcher.join(timeout=5)
            resumed_from = conversation
            conversation = result.conversation_id or conversation
            usage = result.usage
            if caps.cumulative_usage and resumed_from and resumed_from == conversation:
                usage = usage_delta(self._conversation_usage(task_id, conversation, run_id), result.usage)
            self.store.update_run(
                run_id, status=result.status.value, conversation_id=conversation, ended_at=now(),
                classification=result.classification.value, usage=usage, report=result.report,
                transcript_path=result.transcript_path, error=result.error,
            )
            if self.store.get_task(task_id).status == TaskStatus.CANCELLED:
                outcome.status, outcome.message = "CANCELLED", (
                    "cancelled while running; anything the executor changed before stopping was left in place")
                return outcome

            changed = evidence.snapshot(root).tree != before.tree
            if result.status == ExecStatus.SUCCEEDED or (result.status == ExecStatus.DENIED and changed):
                note = f" (it also tried denied actions: {', '.join(result.denied_actions)})" if result.denied_actions else ""
                self.store.transition_task(task_id, TaskStatus.EXECUTED, "engine", "executor finished" + note)
            elif result.status == ExecStatus.DENIED and not denied_retried:
                denied_retried = True
                self._bounce(task_id, f"executor tried denied actions without changing anything: {result.error}")
                prompt = prompts.denied_retry(task, caps, checks)
                kind = "denied_retry"
                continue
            elif result.classification == Classification.TRANSIENT and transient_used < limits("transient_retries"):
                transient_used += 1
                delay = limits("retry_backoff_seconds") * transient_used
                self._bounce(task_id, f"transient failure, retry {transient_used} in {delay}s: {result.error}")
                self.sleep(delay)
                kind = "retry"
                continue
            else:
                reason = f"{result.status.value} ({result.classification.value}): {result.error or 'no detail'}"
                self.store.transition_task(task_id, TaskStatus.FAILED, "engine", reason)
                if result.classification == Classification.ENV_BLOCKED:
                    self.store.add_event("task.escalated", "task", task_id, "engine", project.id,
                                         {"reason": result.error, "needs": "a human to fix the environment"})
                outcome.status, outcome.message = "FAILED", reason
                return outcome

            self.store.transition_task(task_id, TaskStatus.VERIFYING, "engine", "collecting evidence")
            ev = evidence.collect(root, baseline, before, task.spec, pcfg, result.report)
            self.store.update_run(run_id, after_tree=ev["after"]["tree"])
            ev["checks"] = evidence.run_checks(root, project, checks, limits("evidence_output_bytes"))
            ev["executor"] = self._executor_evidence(result, limits("evidence_output_bytes"))
            ev["new_branch"] = new_branch
            ev["run_kind"] = kind
            self.store.insert_evidence(run_id, ev)
            outcome.checks = ev["checks"]
            failing = [c for c in ev["checks"] if not c["passed"]]
            fixable = [c for c in failing if not c.get("skipped")]
            if fixable and auto_fixes < limits("auto_fix_attempts") and not ev["scope_violations"]:
                auto_fixes += 1
                names = ", ".join(c["name"] for c in fixable)
                self.store.transition_task(task_id, TaskStatus.READY, "engine",
                                           f"auto-fix {auto_fixes}/{limits('auto_fix_attempts')}: {names} failed")
                prompt = prompts.auto_fix(task, fixable, caps, checks)
                kind = "auto_fix"
                conversation = conversation if caps.supports_resume else None
                continue

            parts = [f"{len(ev['changed_files'])} file(s) changed"]
            parts.append("checks: " + (", ".join(f"{c['name']}={'pass' if c['passed'] else 'FAIL'}"
                                                  for c in ev["checks"]) or "none"))
            if ev["scope_violations"]:
                parts.append(f"{len(ev['scope_violations'])} scope violation(s)")
            if ev["policy_flags"]:
                parts.append("needs human approval: " + ", ".join(f["action"] for f in ev["policy_flags"]))
            summary = "; ".join(parts)
            self.store.transition_task(task_id, TaskStatus.IN_REVIEW, "engine", summary)
            outcome.status, outcome.message = "IN_REVIEW", summary
            return outcome


# -- scheduling ---------------------------------------------------------------


def run_ready(
    paths: Paths,
    cfg: Config,
    project_id: str | None = None,
    task_ids: list[str] | None = None,
    parallel: int | None = None,
    executor_factory: Callable[[str], ExecutionEngine] = executors.get_executor,
    progress: ProgressCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
    executor_override: str | None = None,
) -> list[dict[str, Any]]:
    """Dispatch READY tasks until none are left, honouring global and per-project concurrency.

    Each worker uses its own database connection. Tasks that cannot start (blockers)
    are reported and skipped, not retried in a loop.
    """
    results: list[dict[str, Any]] = []
    limit = parallel or cfg.get("limits", "global_concurrency")
    seen: set[str] = set()
    running: dict[Future[Outcome], tuple[str, str]] = {}

    def work(task_id: str) -> Outcome:
        s = Store(paths.db_file)
        try:
            return Engine(s, paths, cfg, executor_factory, progress, sleep,
                          executor_override=executor_override).dispatch(task_id)
        finally:
            s.close()

    scan = Store(paths.db_file)
    try:
        with ThreadPoolExecutor(max_workers=limit) as pool:
            while True:
                per_project: dict[str, int] = {}
                for _, pid in running.values():
                    per_project[pid] = per_project.get(pid, 0) + 1
                for task in scan.ready_tasks(project_id):
                    if task.id in seen or (task_ids and task.id not in task_ids) or len(running) >= limit:
                        continue
                    project = scan.get_project(task.project_id)
                    cap = effective_config(cfg, project).get("limits", "per_repo_concurrency")
                    if per_project.get(project.id, 0) >= cap:
                        continue
                    seen.add(task.id)
                    probe = Engine(scan, paths, cfg, executor_factory, executor_override=executor_override)
                    problems, _ = probe.preflight(task, include_git=False)
                    if problems:
                        results.append({"task_id": task.id, "status": "SKIPPED", "message": "; ".join(problems)})
                        continue
                    per_project[project.id] = per_project.get(project.id, 0) + 1
                    running[pool.submit(work, task.id)] = (task.id, project.id)
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    tid, _ = running.pop(fut)
                    try:
                        results.append(fut.result().to_dict())
                    except OrchError as e:
                        results.append({"task_id": tid, "status": "ERROR", "message": str(e)})
    finally:
        scan.close()
    return results


# -- recovery -----------------------------------------------------------------


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def recover(store: Store, actor: str, kill: bool = False) -> list[dict[str, Any]]:
    """Reconcile runs whose orchestrating process died (crash, reboot, killed terminal)."""
    report: list[dict[str, Any]] = []
    for run in store.list_runs(status="running"):
        if run["owner_pid"] != os.getpid() and _alive(run["owner_pid"]):
            report.append({"run": run["id"], "task_id": run["task_id"], "result": "still running",
                           "owner_pid": run["owner_pid"]})
            continue
        if _alive(run["pid"]):
            if not kill:
                report.append({"run": run["id"], "task_id": run["task_id"], "result": "executor still alive",
                               "pid": run["pid"], "hint": "rerun with --kill to stop it"})
                continue
            try:
                os.killpg(run["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        store.update_run(run["id"], status="interrupted", ended_at=now(), error="orchestrating process died")
        task = store.get_task(run["task_id"])
        if task.status in ACTIVE:
            store.transition_task(task.id, TaskStatus.INTERRUPTED, actor, "recovered after the engine stopped")
        report.append({"run": run["id"], "task_id": run["task_id"], "result": "interrupted"})
    return report
