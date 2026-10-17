"""Policy decisions. The engine enforces these in code; no model can talk its way past them.

Plan-time checks are a prediction made from what a plan declares (risk, scope,
relevant files). The authoritative check runs after execution against the files
that actually changed (Phase 3), because a declared scope glob can still match
sensitive paths nobody listed.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field

from orch import globs
from orch.config import Config
from orch.models import Project, TaskSpec


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class PathClasses:
    sensitive: list[str] = field(default_factory=list)
    dependency: list[str] = field(default_factory=list)
    migration: list[str] = field(default_factory=list)


def classify_paths(paths: Iterable[str], cfg: Config) -> PathClasses:
    paths = list(paths)
    return PathClasses(
        sensitive=globs.matching(paths, cfg.get("policy", "sensitive_globs")),
        dependency=globs.matching(paths, cfg.get("policy", "dependency_globs")),
        migration=globs.matching(paths, cfg.get("policy", "migration_globs")),
    )


def is_protected_branch(branch: str, cfg: Config) -> bool:
    return any(fnmatch.fnmatchcase(branch, p) for p in cfg.get("policy", "protected_branches"))


def _literal(pattern: str) -> bool:
    return not any(ch in pattern for ch in "*?[")


def approval_reasons(spec: TaskSpec, cfg: Config, architectural_plan: bool = False) -> list[Reason]:
    """Why this task needs a human approval before it may run (empty list = it may run)."""
    reasons: list[Reason] = []

    def need(action: str, detail: str) -> None:
        if cfg.requires_approval(action):
            reasons.append(Reason(action, detail))

    if spec.risk.value in cfg.get("policy", "require_approval_for_risk"):
        reasons.append(Reason(f"risk_{spec.risk.value}", f"task is marked {spec.risk.value} risk"))
    if architectural_plan:
        need("architectural_decision", "the plan records an architectural decision")
    unbounded = [p for p in spec.scope.allow if globs.is_unbounded(p)]
    if unbounded:
        need("unbounded_scope", f"scope allows the whole repository ({', '.join(unbounded)})")

    declared = list(spec.relevant_files) + [p for p in spec.scope.allow if _literal(p)]
    classes = classify_paths(declared, cfg)
    if classes.sensitive:
        need("sensitive_file", ", ".join(sorted(set(classes.sensitive))))
    if classes.dependency:
        need("dependency_change", ", ".join(sorted(set(classes.dependency))))
    if classes.migration:
        need("migration", ", ".join(sorted(set(classes.migration))))
    return reasons


def dispatch_blockers(spec: TaskSpec, project: Project, executor: str, sends_code_off_machine: bool) -> list[Reason]:
    """Conditions that stop a task from being dispatched even when it is approved."""
    blockers: list[Reason] = []
    if project.archived:
        blockers.append(Reason("project_archived", f"project {project.id} is archived"))
    if sends_code_off_machine and not project.allow_external_engine:
        blockers.append(
            Reason(
                "external_engine_not_allowed",
                f"project {project.id} has not opted in to sending code to {executor} "
                f"(orch project engine {project.id} --allow)",
            )
        )
    needed = set(spec.required_checks) | {
        a.verify.command for a in spec.acceptance if a.verify.kind == "command" and a.verify.command
    }
    for name in sorted(needed):
        cmd = project.commands.get(name)
        if cmd is None:
            blockers.append(Reason("command_missing", f"project has no command {name!r}"))
        elif not cmd.confirmed:
            blockers.append(Reason("command_unconfirmed", f"command {name!r} is not confirmed yet"))
    return blockers
