"""Plan import, validation and approval.

Claude writes a plan as JSON (schema: `orch schema plan`). Import validates it
structurally and semantically, evaluates policy for every task, and stores
the plan and its tasks atomically. Tasks that need approval wait in
AWAITING_APPROVAL; the rest are READY (dispatchable once their dependencies
are APPROVED).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from orch import executors, globs, policy
from orch.config import Config
from orch.errors import OrchError, PolicyError, ValidationError
from orch.models import Project, ReviewDecision, Task, TaskSpec, TaskStatus, task_id
from orch.registry import effective_config, require_user
from orch.schema import PLAN_SCHEMA, validate
from orch.store import Store


@dataclass
class ImportResult:
    plan_id: int
    tasks: list[Task]
    warnings: list[str] = field(default_factory=list)


def execution_waves(specs: list[TaskSpec]) -> list[list[str]]:
    """Group task keys into waves: every task's dependencies are in earlier waves.

    Raises ValidationError naming the tasks involved if the dependencies form a cycle.
    """
    deps = {s.key: set(s.depends_on) for s in specs}
    indegree = {k: len(v) for k, v in deps.items()}
    dependents: dict[str, list[str]] = {k: [] for k in deps}
    for k, ds in deps.items():
        for d in ds:
            dependents[d].append(k)
    waves: list[list[str]] = []
    ready = deque(sorted(k for k, n in indegree.items() if n == 0))
    seen = 0
    while ready:
        wave = list(ready)
        ready.clear()
        waves.append(wave)
        seen += len(wave)
        for k in wave:
            for nxt in dependents[k]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
        ready = deque(sorted(ready))
    if seen != len(deps):
        stuck = sorted(k for k, n in indegree.items() if n > 0)
        raise ValidationError("task dependencies contain a cycle", [f"involved: {', '.join(stuck)}"])
    return waves


def validate_plan(data: Any, project: Project | None) -> tuple[list[TaskSpec], list[str]]:
    """Return parsed task specs and warnings, or raise ValidationError listing every problem."""
    errors = validate(data, PLAN_SCHEMA)
    if errors:
        raise ValidationError("plan does not match the plan schema (orch schema plan)", errors)
    specs = [TaskSpec.from_dict(t) for t in data["tasks"]]
    warnings: list[str] = []
    keys = [s.key for s in specs]
    errors += [f"duplicate task key {k!r}" for k in sorted({k for k in keys if keys.count(k) > 1})]
    for s in specs:
        where = f"task {s.key}"
        for d in s.depends_on:
            if d not in keys:
                errors.append(f"{where}: depends on unknown task {d!r}")
            if d == s.key:
                errors.append(f"{where}: depends on itself")
        ids = [a.id for a in s.acceptance]
        errors += [f"{where}: duplicate acceptance id {i!r}" for i in sorted({i for i in ids if ids.count(i) > 1})]
        for pattern in s.scope.allow + s.scope.deny:
            problem = globs.validate_pattern(pattern)
            if problem:
                errors.append(f"{where}: scope {problem}")
        for f in s.relevant_files:
            problem = globs.validate_pattern(f)
            if problem:
                errors.append(f"{where}: relevant_files {problem}")
            elif not globs.match_any(f, s.scope.allow) or globs.match_any(f, s.scope.deny):
                warnings.append(f"{where}: relevant file {f!r} is outside the task's scope (read-only context?)")
        for a in s.acceptance:
            if a.verify.kind == "command" and not a.verify.command:
                errors.append(f"{where}: acceptance {a.id} has verify.kind 'command' but no command name")
            if a.verify.kind == "review" and a.verify.command:
                errors.append(f"{where}: acceptance {a.id} has a command but verify.kind 'review'")
        if project is not None:
            needed = set(s.required_checks) | {a.verify.command for a in s.acceptance if a.verify.command}
            for name in sorted(needed):
                if name not in project.commands:
                    errors.append(
                        f"{where}: command {name!r} is not defined for project {project.id} "
                        f"(defined: {', '.join(sorted(project.commands)) or 'none'})"
                    )
        if not any(a.verify.kind == "command" for a in s.acceptance) and not s.required_checks:
            warnings.append(f"{where}: no command-verified acceptance criteria; review is the only check")
    if not errors:
        try:
            execution_waves(specs)
        except ValidationError as e:
            errors += [str(e).splitlines()[0]] + e.errors
    if errors:
        raise ValidationError("plan is invalid", errors)
    return specs, warnings


def import_plan(store: Store, cfg: Config, data: Any, actor: str) -> ImportResult:
    if not isinstance(data, dict) or "project_id" not in data:
        validate_plan(data, None)  # raises with schema errors
    project = store.get_project(data["project_id"])
    if project.archived:
        raise ValidationError(f"project {project.id} is archived")
    specs, warnings = validate_plan(data, project)
    pcfg = effective_config(cfg, project)
    architectural = bool(data.get("decision", {}).get("architectural"))
    executor = project.executor or pcfg.get("executor", "default")

    reasons_by_key = {s.key: [str(r) for r in policy.approval_reasons(s, pcfg, architectural)] for s in specs}
    try:
        external = executors.get_executor(executor).capabilities().sends_code_off_machine
    except OrchError:
        external = True
    for s in specs:
        for b in policy.dispatch_blockers(s, project, executor, external):
            warnings.append(f"task {s.key}: will not dispatch until resolved: {b}")

    def make_tasks(plan_id: int) -> list[Task]:
        return [
            Task(
                id=task_id(plan_id, s.key),
                plan_id=plan_id,
                project_id=project.id,
                key=s.key,
                status=TaskStatus.AWAITING_APPROVAL if reasons_by_key[s.key] else TaskStatus.READY,
                review_status=ReviewDecision.NONE,
                attempt=0,
                spec=s,
                depends_on=[task_id(plan_id, d) for d in s.depends_on],
                approval_reasons=reasons_by_key[s.key],
            )
            for s in specs
        ]

    plan_id, tasks = store.create_plan(project.id, data["title"], data, actor, make_tasks)
    return ImportResult(plan_id=plan_id, tasks=tasks, warnings=sorted(set(warnings)))


def approve_task(store: Store, tid: str, actor: str, reason: str) -> Task:
    require_user(actor, "approving a task")
    task = store.get_task(tid)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise PolicyError(f"task {tid} is {task.status.value}, not AWAITING_APPROVAL")
    return store.transition_task(
        tid, TaskStatus.READY, actor, reason, expected=TaskStatus.AWAITING_APPROVAL,
        data={"approved_reasons": task.approval_reasons},
    )


def approve_plan(store: Store, plan_id: int, actor: str, reason: str) -> list[Task]:
    require_user(actor, "approving a plan")
    with store.tx():
        waiting = [t for t in store.list_tasks(plan_id=plan_id) if t.status == TaskStatus.AWAITING_APPROVAL]
        return [approve_task(store, t.id, actor, reason) for t in waiting]


def cancel_task(store: Store, tid: str, actor: str, reason: str) -> Task:
    return store.transition_task(tid, TaskStatus.CANCELLED, actor, reason)
