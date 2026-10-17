"""Project registry: register, refresh and configure projects."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from orch.config import KNOWN_EXECUTORS, Config
from orch.discovery import DiscoveryReport, Which, discover
from orch.errors import PolicyError, ValidationError
from orch.models import Actor, Command, Project
from orch.paths import Paths
from orch.store import Store
from orch.util import now

EDITABLE_FIELDS = ("name", "description", "default_branch", "executor", "model")


def require_user(actor: str, what: str) -> None:
    if actor != Actor.USER.value:
        raise PolicyError(f"{what} must be done by the user (actor 'user'), not {actor!r}")


def _unique_id(store: Store, base: str) -> str:
    candidate, n = base, 2
    while store.project_exists(candidate):
        candidate = f"{base[:60]}-{n}"
        n += 1
    return candidate


def _context_template(project: Project, report: DiscoveryReport) -> str:
    lines = [
        f"# {project.name}",
        "",
        "> Project context for orch. Claude reads this when planning and reviewing tasks for",
        "> this project. Keep it short, factual and current. Facts with a source belong in",
        "> `orch context add` entries, which track provenance and go stale automatically.",
        "",
        "## Overview",
        project.description or "TODO: what this project is and who uses it.",
        "",
        f"## Stack (discovered {now()[:10]})",
        f"- Languages: {', '.join(report.languages) or 'unknown'}",
        f"- Frameworks: {', '.join(report.frameworks) or 'none detected'}",
        f"- Package managers: {', '.join(report.package_managers) or 'none detected'}",
    ]
    if report.compose_services:
        lines.append(f"- Compose services: {', '.join(report.compose_services)}")
    lines += [
        "",
        "## Architecture",
        "TODO: main components, boundaries, and how data flows between them.",
        "",
        "## Conventions",
        "TODO: code style, naming, layering, and testing conventions.",
        "",
        "## Constraints",
        "TODO: what must not change (public APIs, schemas, external contracts).",
        "",
        "## Docs to read first",
    ]
    lines += [f"- {d}" for d in report.docs] or ["- none found"]
    return "\n".join(lines) + "\n"


def _write_knowledge(paths: Paths, project: Project, report: DiscoveryReport, seed_context: bool) -> None:
    d = paths.project_dir(project.id)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / "discovery.json").write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n")
    ctx = d / "context.md"
    if seed_context and not ctx.exists():
        ctx.write_text(_context_template(project, report))


def add_project(
    store: Store,
    paths: Paths,
    path: str | Path,
    actor: str,
    project_id: str | None = None,
    name: str | None = None,
    description: str = "",
    which: Which = shutil.which,
) -> tuple[Project, DiscoveryReport]:
    report = discover(path, which)
    for existing in store.list_projects(include_archived=True):
        if existing.root == report.root:
            raise ValidationError(f"{report.root} is already registered as {existing.id!r}")
    if project_id and store.project_exists(project_id):
        raise ValidationError(f"project id {project_id!r} is taken")
    project = Project(
        id=project_id or _unique_id(store, report.suggested_id),
        name=name or report.suggested_name,
        root=report.root,
        description=description,
        remote=report.remote,
        default_branch=report.default_branch,
        languages=report.languages,
        frameworks=report.frameworks,
        package_managers=report.package_managers,
        commands=report.proposed_commands(),
    )
    store.insert_project(project, actor)
    _write_knowledge(paths, project, report, seed_context=True)
    return project, report


def refresh_project(
    store: Store, paths: Paths, project_id: str, actor: str, which: Which = shutil.which
) -> tuple[Project, DiscoveryReport, list[str]]:
    """Re-run discovery. Confirmed commands are never touched; unconfirmed ones follow discovery."""
    project = store.get_project(project_id)
    report = discover(project.root, which)
    changes: list[str] = []
    for attr in ("remote", "default_branch", "languages", "frameworks", "package_managers"):
        new = getattr(report, attr)
        if getattr(project, attr) != new:
            changes.append(f"{attr}: {getattr(project, attr)!r} -> {new!r}")
            setattr(project, attr, new)
    for name, proposed in report.proposed_commands().items():
        current = project.commands.get(name)
        if current is None:
            project.commands[name] = proposed
            changes.append(f"command {name}: added (unconfirmed) {proposed.run!r}")
        elif not current.confirmed and current.source.startswith("discovered:") and current.run != proposed.run:
            project.commands[name] = proposed
            changes.append(f"command {name}: {current.run!r} -> {proposed.run!r} (unconfirmed)")
    store.save_project(project, actor, "project.refreshed", {"changes": changes})
    _write_knowledge(paths, project, report, seed_context=False)
    return project, report, changes


def set_command(
    store: Store,
    project_id: str,
    name: str,
    run: str,
    actor: str,
    cwd: str = ".",
    timeout_seconds: int = 900,
    requires_services: bool = False,
    confirm: bool = False,
) -> Project:
    if confirm:
        require_user(actor, "confirming a command")
    project = store.get_project(project_id)
    cmd = Command(
        run=run, cwd=cwd, timeout_seconds=timeout_seconds, requires_services=requires_services,
        source=actor, confidence="high", confirmed=confirm,
    )
    errors = cmd.validate(name)
    if errors:
        raise ValidationError("invalid command", errors)
    project.commands[name] = cmd
    return store.save_project(project, actor, "project.command_set", {"name": name, "run": run, "confirmed": confirm})


def remove_command(store: Store, project_id: str, name: str, actor: str) -> Project:
    project = store.get_project(project_id)
    if name not in project.commands:
        raise ValidationError(f"project {project_id} has no command {name!r}")
    del project.commands[name]
    return store.save_project(project, actor, "project.command_removed", {"name": name})


def confirm_commands(store: Store, project_id: str, names: list[str] | None, actor: str) -> Project:
    """Mark commands as trusted to run. Only the user can do this."""
    require_user(actor, "confirming commands")
    project = store.get_project(project_id)
    targets = names or list(project.commands)
    missing = [n for n in targets if n not in project.commands]
    if missing:
        raise ValidationError(f"unknown commands: {missing}")
    for n in targets:
        project.commands[n].confirmed = True
    confirmed = {n: project.commands[n].run for n in targets}
    return store.save_project(project, actor, "project.commands_confirmed", {"commands": confirmed})


def set_engine(store: Store, project_id: str, allow: bool, actor: str) -> Project:
    """Opt a project in or out of external execution engines (which send its code off this machine)."""
    if allow:
        require_user(actor, "allowing an external engine")
    project = store.get_project(project_id)
    project.allow_external_engine = allow
    return store.save_project(project, actor, "project.engine", {"allow_external_engine": allow})


def set_field(store: Store, project_id: str, key: str, value: str, actor: str) -> Project:
    if key not in EDITABLE_FIELDS:
        raise ValidationError(f"cannot set {key!r}; editable fields: {', '.join(EDITABLE_FIELDS)}")
    if key == "executor" and value not in KNOWN_EXECUTORS:
        raise ValidationError(f"executor must be one of {sorted(KNOWN_EXECUTORS)}")
    project = store.get_project(project_id)
    old = getattr(project, key)
    nullable = key in ("executor", "model", "default_branch")
    setattr(project, key, (value or None) if nullable else value)
    return store.save_project(project, actor, "project.updated", {"field": key, "from": old, "to": value})


def set_override(
    store: Store, cfg: Config, project_id: str, section: str, key: str, raw_value: str, actor: str
) -> Project:
    """Override one config key for a project. raw_value is JSON (a bare word is taken as a string)."""
    try:
        value: Any = json.loads(raw_value)
    except json.JSONDecodeError:
        value = raw_value
    project = store.get_project(project_id)
    overrides = json.loads(json.dumps(project.overrides))
    overrides.setdefault(section, {})[key] = value
    effective = cfg.with_overrides(overrides, where=f"project {project_id}")
    if section == "policy":
        loosened = [
            a for a in effective.get("policy", "auto_actions") if a not in cfg.get("policy", "auto_actions")
        ]
        if loosened:
            require_user(actor, f"making {', '.join(loosened)} automatic")
    project.overrides = overrides
    return store.save_project(project, actor, "project.override", {"section": section, "key": key, "value": value})


def archive_project(store: Store, project_id: str, actor: str) -> Project:
    project = store.get_project(project_id)
    project.archived = True
    return store.save_project(project, actor, "project.archived")


def effective_config(cfg: Config, project: Project) -> Config:
    return cfg.with_overrides(project.overrides, where=f"project {project.id}") if project.overrides else cfg
