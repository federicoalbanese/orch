"""The `orch` command line. Human-readable by default, JSON with --json.

Who is acting matters (some actions only a user may take). The actor comes
from --actor, or the ORCH_ACTOR environment variable, defaulting to "user".
When ORCH_ACTOR is set (for example to "claude" in Claude Code's environment),
--actor cannot override it, so an agent cannot record itself as the user.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from orch import __version__, context, executors, plans, registry, review, skill, workflow
from orch.config import ACTIONS, NEVER_AUTOMATIC, Config, default_config_text, load_config
from orch.discovery import DiscoveryReport, discover
from orch.errors import NotFoundError, OrchError, ValidationError
from orch.models import Actor, Project, Task, TaskStatus
from orch.paths import Paths, resolve_paths
from orch.schema import SCHEMAS
from orch.statemachine import WAITING_ON_HUMAN
from orch.store import Store


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.paths: Paths = resolve_paths(args.home)
        self._store: Store | None = None
        self._config: Config | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self.paths.ensure()
            self._store = Store(self.paths.db_file)
        return self._store

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.paths.config_file)
        return self._config

    @property
    def actor(self) -> str:
        return self.args.actor

    def close(self) -> None:
        if self._store is not None:
            self._store.close()

    def out(self, data: Any, human: Callable[[], str] | str | None = None) -> None:
        if self.args.json or human is None:
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        else:
            text = human() if callable(human) else human
            if text:
                print(text)


# -- formatting helpers -------------------------------------------------------


def table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _plan_id(raw: str) -> int:
    try:
        return int(raw.upper().removeprefix("P"))
    except ValueError as e:
        raise ValidationError(f"not a plan id: {raw!r} (expected P<number>)") from e


def _read_json_arg(source: str) -> Any:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValidationError(f"{source}: invalid JSON", [f"line {e.lineno} column {e.colno}: {e.msg}"]) from e


def render_discovery(r: DiscoveryReport) -> str:
    lines = [
        f"Root:        {r.root}",
        f"Suggested:   id={r.suggested_id}  name={r.suggested_name}",
        f"Git:         {'yes' if r.is_git else 'no'}"
        + (f"  remote={r.remote or '-'}  default={r.default_branch or '-'}  current={r.current_branch or '-'}"
           f"  dirty={r.dirty}" if r.is_git else ""),
        f"Languages:   {', '.join(r.languages) or '-'}",
        f"Frameworks:  {', '.join(r.frameworks) or '-'}",
        f"Packages:    {', '.join(r.package_managers) or '-'}",
        f"Markers:     {', '.join(r.markers) or '-'}",
    ]
    if r.compose_files:
        lines.append(f"Compose:     {', '.join(r.compose_files)}  services={', '.join(r.compose_services) or '?'}")
    if r.makefile_targets:
        lines.append(f"Make:        {', '.join(r.makefile_targets[:25])}" + (" ..." if len(r.makefile_targets) > 25 else ""))
    lines.append(f"Docs:        {', '.join(r.docs) or '-'}")
    lines.append(f"Test dirs:   {', '.join(r.test_dirs) or '-'}")
    rows = []
    for name, cands in sorted(r.candidates.items()):
        for i, c in enumerate(cands):
            rows.append([name if i == 0 else "", "*" if i == 0 else "", c.run, c.confidence,
                         "yes" if c.requires_services else "", c.source, c.notes])
    lines += ["", "Proposed commands (* = proposed; nothing runs until you confirm it):"]
    lines.append(table(rows, ["command", "", "run", "confidence", "services", "source", "notes"]) if rows else "  none")
    if r.warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in r.warnings]
    return "\n".join(lines)


def render_project(p: Project, counts: dict[str, int], knowledge_dir: Path) -> str:
    lines = [
        f"{p.id}  ({p.name}){'  [archived]' if p.archived else ''}",
        f"  root:             {p.root}",
        f"  remote:           {p.remote or '-'}",
        f"  default branch:   {p.default_branch or '-'}",
        f"  stack:            {', '.join(p.languages + p.frameworks) or '-'}",
        f"  external engine:  {'ALLOWED' if p.allow_external_engine else 'not allowed (code stays on this machine)'}",
        f"  executor/model:   {p.executor or '(default)'} / {p.model or '(default)'}",
        f"  overrides:        {json.dumps(p.overrides) if p.overrides else '-'}",
        f"  knowledge:        {knowledge_dir}",
        f"  description:      {p.description or '-'}",
        f"  tasks:            {', '.join(f'{k}={v}' for k, v in sorted(counts.items())) or 'none'}",
        "",
    ]
    rows = [[n, c.run, "yes" if c.confirmed else "NO", c.confidence, c.timeout_seconds,
             "yes" if c.requires_services else "", c.source] for n, c in sorted(p.commands.items())]
    lines.append(table(rows, ["command", "run", "confirmed", "confidence", "timeout", "services", "source"])
                 if rows else "no commands")
    return "\n".join(lines)


def render_task(t: Task, runs: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> str:
    lines = [
        f"{t.id}  {t.spec.title}",
        f"  status: {t.status.value}   review: {t.review_status.value}   attempt: {t.attempt}   risk: {t.spec.risk.value}",
        f"  depends on: {', '.join(t.depends_on) or '-'}",
    ]
    if t.approval_reasons:
        lines.append("  needs approval because:")
        lines += [f"    - {r}" for r in t.approval_reasons]
    lines += ["", context.render_task(t)]
    if runs:
        lines += ["", table([[r["id"], r["attempt"], r["kind"], r["executor"], r["status"], r["started_at"]]
                             for r in runs], ["run", "attempt", "kind", "executor", "status", "started"])]
    if reviews:
        lines += ["", table([[r["id"], r["attempt"], r["decision"], r["reviewer"], r["created_at"]]
                             for r in reviews], ["review", "attempt", "decision", "reviewer", "at"])]
    return "\n".join(lines)


def task_rows(tasks: list[Task]) -> str:
    if not tasks:
        return "no tasks"
    return table(
        [[t.id, t.status.value, t.review_status.value, t.spec.risk.value, t.attempt, ",".join(t.depends_on), t.spec.title]
         for t in tasks],
        ["task", "status", "review", "risk", "attempt", "depends on", "title"],
    )


# -- commands -----------------------------------------------------------------


def cmd_init(app: App) -> int:
    app.paths.ensure()
    created = False
    if not app.paths.config_file.exists():
        app.paths.config_file.write_text(default_config_text(), encoding="utf-8")
        created = True
    version = app.store.schema_version
    data = {"config": str(app.paths.config_file), "config_created": created,
            "data": str(app.paths.data_dir), "db_schema": version}
    app.out(data, lambda: (
        f"config:  {data['config']}{' (created with defaults)' if created else ' (kept)'}\n"
        f"data:    {data['data']}\n"
        f"db:      schema v{version}"
    ))
    return 0


def cmd_doctor(app: App) -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "critical": critical})

    check("python", sys.version_info >= (3, 11), sys.version.split()[0])
    git = shutil.which("git")
    check("git", git is not None, git or "not found")
    check("sqlite", sqlite3.sqlite_version_info >= (3, 35), sqlite3.sqlite_version)
    try:
        cfg = app.config
        check("config", True, str(cfg.source or "defaults (no config file)"))
    except OrchError as e:
        check("config", False, str(e))
    try:
        app.paths.ensure()
        probe = app.paths.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        check("data dir", True, str(app.paths.data_dir))
        check("database", True, f"schema v{app.store.schema_version} at {app.paths.db_file}")
    except (OSError, OrchError, sqlite3.Error) as e:
        check("data dir", False, str(e))
    for name in executors.available():
        h = executors.get_executor(name).health()
        check(f"executor {name}", h.ok, h.detail, critical=False)
    try:
        stuck = [r for r in app.store.list_runs(status="running") if not workflow._alive(r["owner_pid"])]
        check("runs", not stuck, f"{len(stuck)} run(s) left by a stopped engine (orch recover)" if stuck else "no orphaned runs",
              critical=False)
    except (OrchError, sqlite3.Error) as e:
        check("runs", False, str(e), critical=False)
    ok = all(c["ok"] for c in checks if c["critical"])
    app.out({"ok": ok, "checks": checks}, lambda: table(
        [["ok" if c["ok"] else ("FAIL" if c["critical"] else "warn"), c["check"], c["detail"]] for c in checks],
        ["", "check", "detail"],
    ))
    return 0 if ok else 1


def cmd_config(app: App) -> int:
    if app.args.config_cmd == "path":
        app.out({"config": str(app.paths.config_file)}, str(app.paths.config_file))
        return 0
    cfg = app.config
    data = {"source": str(cfg.source) if cfg.source else None, "config": cfg.data,
            "actions": ACTIONS, "never_automatic": sorted(NEVER_AUTOMATIC)}
    app.out(data, lambda: json.dumps(data, indent=2))
    return 0


def cmd_schema(app: App) -> int:
    print(json.dumps(SCHEMAS[app.args.name], indent=2))
    return 0


def cmd_project(app: App) -> int:
    a, s = app.args, None
    sub = a.project_cmd
    if sub == "discover":
        report = discover(a.path)
        app.out(report.to_dict(), lambda: render_discovery(report))
        return 0
    s = app.store
    if sub == "add":
        project, report = registry.add_project(s, app.paths, a.path, app.actor, a.id, a.name, a.description or "")
        app.out({"project": project.to_dict(), "discovery": report.to_dict()}, lambda: (
            render_discovery(report) + f"\n\nRegistered as {project.id!r}.\n"
            f"Next: review the proposed commands, then `orch project command confirm {project.id}`.\n"
            f"Edit the project context at {app.paths.project_dir(project.id) / 'context.md'}"
        ))
    elif sub == "list":
        projects = s.list_projects(include_archived=a.all)
        counts = s.task_counts()
        app.out([p.to_dict() for p in projects], lambda: table(
            [[p.id, p.name, ", ".join(p.languages + p.frameworks), "yes" if p.allow_external_engine else "no",
              sum(1 for c in p.commands.values() if c.confirmed), len(p.commands),
              sum(counts.get(p.id, {}).values()), p.root] for p in projects],
            ["id", "name", "stack", "engine", "confirmed", "cmds", "tasks", "root"],
        ) if projects else "no projects (orch project add <path>)")
    elif sub == "show":
        p = s.get_project(a.id)
        counts = s.task_counts().get(p.id, {})
        app.out({**p.to_dict(), "task_counts": counts, "knowledge_dir": str(app.paths.project_dir(p.id))},
                lambda: render_project(p, counts, app.paths.project_dir(p.id)))
    elif sub == "refresh":
        p, _, changes = registry.refresh_project(s, app.paths, a.id, app.actor)
        app.out({"project": p.to_dict(), "changes": changes},
                lambda: "\n".join(changes) if changes else "no changes")
    elif sub == "set":
        p = registry.set_field(s, a.id, a.field, a.value, app.actor)
        app.out(p.to_dict(), f"{a.id}.{a.field} = {a.value!r}")
    elif sub == "override":
        section, _, key = a.key.partition(".")
        p = registry.set_override(s, app.config, a.id, section, key, a.value, app.actor)
        app.out(p.to_dict(), f"{a.id}: override {section}.{key} = {p.overrides[section][key]!r}")
    elif sub == "command":
        if a.command_cmd == "set":
            p = registry.set_command(s, a.id, a.name, a.run, app.actor, a.cwd, a.timeout, a.requires_services, a.confirm)
            app.out(p.to_dict(), f"{a.id}: command {a.name} = {a.run!r}{' (confirmed)' if a.confirm else ' (unconfirmed)'}")
        elif a.command_cmd == "remove":
            p = registry.remove_command(s, a.id, a.name, app.actor)
            app.out(p.to_dict(), f"{a.id}: command {a.name} removed")
        elif a.command_cmd == "confirm":
            p = registry.confirm_commands(s, a.id, a.names or None, app.actor)
            confirmed = [n for n, c in sorted(p.commands.items()) if c.confirmed]
            app.out(p.to_dict(), f"{a.id}: confirmed {', '.join(confirmed)}" if confirmed
                    else f"{a.id}: no commands to confirm (add one with: orch project command set)")
    elif sub == "engine":
        p = registry.set_engine(s, a.id, a.allow, app.actor)
        app.out(p.to_dict(), f"{a.id}: external engine {'ALLOWED' if a.allow else 'not allowed'}")
    elif sub == "archive":
        if not a.yes:
            raise ValidationError("archiving needs --yes")
        p = registry.archive_project(s, a.id, app.actor)
        app.out(p.to_dict(), f"{a.id}: archived")
    return 0


def cmd_plan(app: App) -> int:
    a, s = app.args, app.store
    if a.plan_cmd == "import":
        result = plans.import_plan(s, app.config, _read_json_arg(a.file), app.actor)
        data = {"plan_id": f"P{result.plan_id}", "tasks": [t.to_dict() for t in result.tasks],
                "warnings": result.warnings}
        app.out(data, lambda: f"Imported P{result.plan_id}\n\n" + task_rows(result.tasks) + (
            "\n\nWarnings:\n" + "\n".join(f"  - {w}" for w in result.warnings) if result.warnings else ""))
    elif a.plan_cmd == "list":
        rows = s.list_plans(a.project)
        app.out(rows, lambda: table([[f"P{r['id']}", r["project_id"], f"{r['approved'] or 0}/{r['tasks']}",
                                      r["created_by"], r["created_at"], r["title"]] for r in rows],
                                    ["plan", "project", "approved", "by", "created", "title"]) if rows else "no plans")
    elif a.plan_cmd == "show":
        plan = s.get_plan(_plan_id(a.id))
        specs = [t.spec for t in plan["tasks"]]
        waves = plans.execution_waves(specs)
        data = {**plan, "tasks": [t.to_dict() for t in plan["tasks"]], "waves": waves}
        d = plan["data"]
        app.out(data, lambda: "\n".join([
            f"P{plan['id']}  {plan['title']}  ({plan['project_id']})",
            f"Objective: {d['objective']}",
            *([f"Decision: {d['decision']['summary']}"
               + ("  [ARCHITECTURAL]" if d['decision'].get('architectural') else "")] if d.get("decision") else []),
            *([f"Risks: {'; '.join(d['risks'])}"] if d.get("risks") else []),
            "Execution waves: " + "  ->  ".join("[" + ", ".join(w) + "]" for w in waves),
            "",
            task_rows(plan["tasks"]),
        ]))
    elif a.plan_cmd == "approve":
        approved = plans.approve_plan(s, _plan_id(a.id), app.actor, a.reason)
        app.out([t.to_dict() for t in approved],
                f"approved {len(approved)} task(s)" + (": " + ", ".join(t.id for t in approved) if approved else ""))
    return 0


def cmd_task(app: App) -> int:
    a, s = app.args, app.store
    if a.task_cmd == "list":
        statuses = [TaskStatus(x.upper()) for x in a.status] if a.status else None
        tasks = s.list_tasks(a.project, _plan_id(a.plan) if a.plan else None, statuses)
        app.out([t.to_dict() for t in tasks], lambda: task_rows(tasks))
    elif a.task_cmd == "show":
        t = s.get_task(a.id)
        runs, reviews = s.list_runs(t.id), s.list_reviews(t.id)
        app.out({**t.to_dict(), "runs": runs, "reviews": reviews}, lambda: render_task(t, runs, reviews))
    elif a.task_cmd == "ready":
        tasks = s.ready_tasks(a.project)
        app.out([t.to_dict() for t in tasks], lambda: task_rows(tasks))
    elif a.task_cmd == "approve":
        t = plans.approve_task(s, a.id, app.actor, a.reason)
        app.out(t.to_dict(), f"{t.id}: {t.status.value}")
    elif a.task_cmd == "cancel":
        t = plans.cancel_task(s, a.id, app.actor, a.reason)
        app.out(t.to_dict(), f"{t.id}: {t.status.value}")
    elif a.task_cmd == "accept":
        t = review.accept(s, a.id, app.actor, a.reason)
        app.out(t.to_dict(), f"{t.id}: {t.status.value}")
    elif a.task_cmd == "retry":
        t = review.retry(s, app.config, a.id, app.actor, a.reason)
        app.out(t.to_dict(), f"{t.id}: {t.status.value}" + (" (correction pending)" if t.review_status.value == "NEEDS_CORRECTION" else ""))
    return 0


def _progress_printer(app: App) -> workflow.ProgressCallback | None:
    if app.args.json or app.args.quiet:
        return None

    def show(task_id: str, e: dict[str, Any]) -> None:
        kind = e.get("event")
        if kind == "dispatched":
            print(f"{task_id}  run {e['run']}: {e['kind']} attempt {e['attempt']}", flush=True)
        elif kind == "tool" and e.get("state") == "DONE":
            print(f"{task_id}    tool {e.get('name')}", flush=True)
        elif kind == "waiting_for_human":
            print(f"{task_id}    waiting for you: paste {e['prompt']} into Antigravity, then save report.json there",
                  flush=True)
        elif kind == "result":
            print(f"{task_id}    executor: {e.get('status')}", flush=True)

    return show


def cmd_run(app: App) -> int:
    a = app.args
    app.store  # creates the data directory and database
    results = workflow.run_ready(app.paths, app.config, a.project, a.task or None, a.parallel,
                                 progress=_progress_printer(app), executor_override=a.executor)
    app.out(results, lambda: table([[r["task_id"], r["status"], r["message"]] for r in results],
                                   ["task", "result", "detail"]) if results else "nothing to dispatch (orch task ready)")
    return 0 if all(r["status"] in ("IN_REVIEW", "CANCELLED") for r in results) else 1


def cmd_review(app: App) -> int:
    a, s = app.args, app.store
    if a.review_cmd == "show":
        bundle = review.build_bundle(s, app.config, a.id)
        app.out(bundle, lambda: review.render_bundle(bundle))
    elif a.review_cmd == "decide":
        t, review_id = review.decide(s, app.config, a.id, _read_json_arg(a.file), app.actor)
        app.out({"review": review_id, "task": t.to_dict()},
                f"review #{review_id} recorded; {t.id} is now {t.status.value}")
    return 0


def cmd_skill(app: App) -> int:
    if app.args.skill_cmd == "path":
        path = skill.bundled_skill()
        app.out({"bundled": str(path), "default_destination": str(skill.default_destination())}, str(path))
        return 0
    target, state = skill.install(Path(app.args.dest) if app.args.dest else None, force=app.args.force)
    app.out({"path": str(target), "result": state}, f"skill {state}: {target}")
    return 0


def cmd_recover(app: App) -> int:
    report = workflow.recover(app.store, app.actor, kill=app.args.kill)
    app.out(report, lambda: table([[r["run"], r["task_id"], r["result"], r.get("hint", "")] for r in report],
                                  ["run", "task", "result", "hint"]) if report else "nothing to recover")
    return 0


def cmd_context(app: App) -> int:
    a, s = app.args, app.store
    if a.context_cmd == "add":
        if not a.shared and not a.project:
            raise ValidationError("give a project id or --shared")
        project = None if a.shared else s.get_project(a.project)
        body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else (a.body or "")
        entry_id = context.add_entry(s, project, a.kind, a.title, body, a.source, app.actor, a.ref)
        app.out({"id": entry_id}, f"added context entry #{entry_id}")
    elif a.context_cmd == "list":
        statuses = None if a.all else ["active"]
        entries = s.list_context_entries(None if a.shared else a.project, a.kind, statuses)
        app.out(entries, lambda: table(
            [[e["id"], e["kind"], e["status"], e["verified_at"][:10], (e["source_commit"] or "")[:10],
              ",".join(e["refs"]), e["title"]] for e in entries],
            ["id", "kind", "status", "verified", "commit", "refs", "title"]) if entries else "no entries")
    elif a.context_cmd == "check":
        results = context.check_staleness(s, s.get_project(a.project), app.actor)
        app.out(results, lambda: table([[r["id"], r["result"], r["title"]] for r in results],
                                       ["id", "result", "title"]) if results else "no entries with file refs")
    elif a.context_cmd == "reverify":
        context.reverify(s, s.get_project(a.project), a.entry, app.actor)
        app.out({"id": a.entry, "status": "active"}, f"entry #{a.entry} re-verified")
    elif a.context_cmd == "show":
        project = s.get_project(a.project)
        task = s.get_task(a.task) if a.task else None
        budget = a.budget or app.config.get("limits", "context_budget_chars")
        bundle = context.build_context(s, app.paths, project, budget, task)
        app.out(bundle.to_dict(), bundle.render)
    return 0


def cmd_status(app: App) -> int:
    s = app.store
    projects = s.list_projects()
    counts = s.task_counts()
    waiting = s.list_tasks(statuses=sorted(WAITING_ON_HUMAN, key=lambda x: x.value))
    ready = s.ready_tasks()
    active = s.list_tasks(statuses=[TaskStatus.RUNNING, TaskStatus.EXECUTED, TaskStatus.VERIFYING,
                                    TaskStatus.NEEDS_CORRECTION])
    in_review = s.list_tasks(statuses=[TaskStatus.IN_REVIEW])
    data = {"projects": {p.id: counts.get(p.id, {}) for p in projects},
            "waiting_on_human": [t.to_dict() for t in waiting],
            "ready": [t.id for t in ready], "active": [t.to_dict() for t in active],
            "in_review": [t.id for t in in_review]}

    def human() -> str:
        out = ["Projects:", table([[p.id, "yes" if p.allow_external_engine else "no",
                                    ", ".join(f"{k}={v}" for k, v in sorted(counts.get(p.id, {}).items())) or "-"]
                                   for p in projects], ["project", "engine", "tasks"]) if projects else "  none"]
        out += ["", "Waiting on you:"]
        out += [f"  {t.id}  {t.status.value}  {t.spec.title}" + (f"  ({'; '.join(t.approval_reasons)})"
                if t.approval_reasons and t.status == TaskStatus.AWAITING_APPROVAL else "") for t in waiting] or ["  nothing"]
        out += ["", "Waiting for review (orch review show <task>):"]
        out += [f"  {t.id}  {t.spec.title}" for t in in_review] or ["  nothing"]
        out += ["", "In progress:"] + ([f"  {t.id}  {t.status.value}  {t.spec.title}" for t in active] or ["  nothing"])
        out += ["", "Ready to dispatch: " + (", ".join(t.id for t in ready) or "none")]
        return "\n".join(out)

    app.out(data, human)
    return 0


def cmd_log(app: App) -> int:
    a = app.args
    if a.task:
        events = app.store.list_events(subject_type="task", subject_id=a.task, limit=a.limit)
    else:
        events = app.store.list_events(project_id=a.project, limit=a.limit)
    app.out(events, lambda: table(
        [[e["id"], e["ts"], e["actor"], e["kind"], e["subject_id"],
          json.dumps(e["data"], ensure_ascii=False)[:100]] for e in events],
        ["#", "time", "actor", "event", "subject", "data"]) if events else "no events")
    return 0


# -- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch", description="Claude plans and reviews; an execution engine implements.")
    p.add_argument("--version", action="version", version=f"orch {__version__}")
    p.add_argument("--home", help="keep config and data under this directory (or set ORCH_HOME)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--actor", choices=[x.value for x in Actor], help="who is acting (default: $ORCH_ACTOR or user)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the config file and database")
    sub.add_parser("doctor", help="check the environment")
    c = sub.add_parser("config", help="show the effective configuration")
    c.add_argument("config_cmd", nargs="?", choices=["show", "path"], default="show")
    sc = sub.add_parser("schema", help="print a data-contract JSON schema")
    sc.add_argument("name", choices=sorted(SCHEMAS))
    sub.add_parser("status", help="overview of projects and tasks")
    lg = sub.add_parser("log", help="audit log")
    lg.add_argument("--project")
    lg.add_argument("--task")
    lg.add_argument("--limit", type=int, default=30)

    pr = sub.add_parser("project", help="register and configure projects").add_subparsers(dest="project_cmd", required=True)
    x = pr.add_parser("discover", help="inspect a repository (read-only, nothing is saved)")
    x.add_argument("path")
    x = pr.add_parser("add", help="register a repository")
    x.add_argument("path")
    x.add_argument("--id")
    x.add_argument("--name")
    x.add_argument("--description")
    x = pr.add_parser("list")
    x.add_argument("--all", action="store_true", help="include archived")
    pr.add_parser("show").add_argument("id")
    pr.add_parser("refresh", help="re-run discovery").add_argument("id")
    x = pr.add_parser("set", help=f"set one of: {', '.join(registry.EDITABLE_FIELDS)}")
    x.add_argument("id")
    x.add_argument("field", choices=registry.EDITABLE_FIELDS)
    x.add_argument("value")
    x = pr.add_parser("override", help="override a config key for this project, e.g. limits.max_review_cycles 5")
    x.add_argument("id")
    x.add_argument("key", help="section.key")
    x.add_argument("value", help="JSON value")
    cm = pr.add_parser("command", help="manage the project's build/test/lint commands").add_subparsers(
        dest="command_cmd", required=True)
    x = cm.add_parser("set")
    x.add_argument("id")
    x.add_argument("name")
    x.add_argument("run")
    x.add_argument("--cwd", default=".")
    x.add_argument("--timeout", type=int, default=900)
    x.add_argument("--requires-services", action="store_true")
    x.add_argument("--confirm", action="store_true", help="also mark it trusted to run (user only)")
    x = cm.add_parser("remove")
    x.add_argument("id")
    x.add_argument("name")
    x = cm.add_parser("confirm", help="mark commands as trusted to run (user only); all if none named")
    x.add_argument("id")
    x.add_argument("names", nargs="*")
    x = pr.add_parser("engine", help="opt a project in/out of external execution engines")
    x.add_argument("id")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--allow", action="store_true")
    g.add_argument("--deny", dest="allow", action="store_false")
    x = pr.add_parser("archive")
    x.add_argument("id")
    x.add_argument("--yes", action="store_true")

    pl = sub.add_parser("plan", help="import and inspect plans").add_subparsers(dest="plan_cmd", required=True)
    pl.add_parser("import", help="import a plan JSON file ('-' for stdin)").add_argument("file")
    pl.add_parser("list").add_argument("--project")
    pl.add_parser("show").add_argument("id")
    x = pl.add_parser("approve", help="approve every task of a plan that awaits approval (user only)")
    x.add_argument("id")
    x.add_argument("--reason", required=True)

    tk = sub.add_parser("task", help="inspect and control tasks").add_subparsers(dest="task_cmd", required=True)
    x = tk.add_parser("list")
    x.add_argument("--project")
    x.add_argument("--plan")
    x.add_argument("--status", nargs="*", choices=[st.value for st in TaskStatus] + [st.value.lower() for st in TaskStatus])
    tk.add_parser("show").add_argument("id")
    tk.add_parser("ready", help="tasks that may be dispatched now").add_argument("--project")
    for name, helptext in (
        ("approve", "approve a task that awaits approval (user only)"),
        ("cancel", "cancel a task (a running executor is stopped)"),
        ("accept", "accept a result that needs human approval (user only)"),
        ("retry", "put a FAILED, INTERRUPTED or BLOCKED task back to READY"),
    ):
        x = tk.add_parser(name, help=helptext)
        x.add_argument("id")
        x.add_argument("--reason", required=True)

    rn = sub.add_parser("run", help="dispatch READY tasks to the executor and collect evidence")
    rn.add_argument("--task", action="append", help="only these tasks (repeatable)")
    rn.add_argument("--project")
    rn.add_argument("--parallel", type=int, help="override limits.global_concurrency")
    rn.add_argument("--executor", choices=executors.available(), help="override the project's executor")
    rn.add_argument("--quiet", action="store_true", help="no progress lines")
    rv = sub.add_parser("review", help="review evidence and record decisions").add_subparsers(dest="review_cmd", required=True)
    rv.add_parser("show", help="the evidence bundle for a task").add_argument("id")
    x = rv.add_parser("decide", help="record a review decision (JSON file or '-', schema: orch schema review)")
    x.add_argument("id")
    x.add_argument("--file", required=True)
    sub.add_parser("recover", help="reconcile runs left behind by a stopped engine").add_argument(
        "--kill", action="store_true", help="also stop executor processes that are still alive")

    sk = sub.add_parser("skill", help="the Claude Code skill that drives orch").add_subparsers(dest="skill_cmd", required=True)
    x = sk.add_parser("install", help="install the orchestrate skill into ~/.claude/skills")
    x.add_argument("--dest", help="install somewhere else (a directory or a SKILL.md path)")
    x.add_argument("--force", action="store_true", help="replace a modified copy")
    sk.add_parser("path", help="print where the bundled skill lives")

    cx = sub.add_parser("context", help="project knowledge with provenance").add_subparsers(dest="context_cmd", required=True)
    x = cx.add_parser("add")
    x.add_argument("project", nargs="?")
    x.add_argument("--shared", action="store_true", help="shared engineering knowledge, not tied to a project")
    x.add_argument("--kind", required=True, choices=context.KINDS)
    x.add_argument("--title", required=True)
    gb = x.add_mutually_exclusive_group(required=True)
    gb.add_argument("--body")
    gb.add_argument("--body-file")
    x.add_argument("--source", required=True, help="where this fact comes from (file, URL, review id, 'user')")
    x.add_argument("--ref", action="append", default=[], help="repository path/glob this fact depends on (repeatable)")
    x = cx.add_parser("list")
    x.add_argument("project", nargs="?")
    x.add_argument("--shared", action="store_true")
    x.add_argument("--kind", action="append", choices=context.KINDS)
    x.add_argument("--all", action="store_true", help="include stale and superseded")
    cx.add_parser("check", help="mark entries stale whose referenced files changed").add_argument("project")
    x = cx.add_parser("reverify")
    x.add_argument("project")
    x.add_argument("entry", type=int)
    x = cx.add_parser("show", help="the layered context bundle a task would get")
    x.add_argument("project")
    x.add_argument("--task")
    x.add_argument("--budget", type=int)
    return p


HANDLERS: dict[str, Callable[[App], int]] = {
    "init": cmd_init, "doctor": cmd_doctor, "config": cmd_config, "schema": cmd_schema,
    "project": cmd_project, "plan": cmd_plan, "task": cmd_task, "context": cmd_context,
    "status": cmd_status, "log": cmd_log, "run": cmd_run, "review": cmd_review, "recover": cmd_recover,
    "skill": cmd_skill,
}


def resolve_actor(flag: str | None, env: Mapping[str, str] = os.environ) -> str:
    fixed = env.get("ORCH_ACTOR")
    if fixed:
        if fixed not in {x.value for x in Actor}:
            raise ValidationError(f"ORCH_ACTOR={fixed!r} is not one of user, claude, engine")
        if flag and flag != fixed:
            raise ValidationError(f"--actor {flag} conflicts with ORCH_ACTOR={fixed}; the environment wins")
        return fixed
    return flag or Actor.USER.value


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app: App | None = None
    try:
        args.actor = resolve_actor(args.actor)
        app = App(args)
        return HANDLERS[args.cmd](app)
    except OrchError as e:
        if args.json:
            payload: dict[str, Any] = {"error": type(e).__name__, "message": str(e).splitlines()[0]}
            if isinstance(e, ValidationError):
                payload["details"] = e.errors
            print(json.dumps(payload, indent=2), file=sys.stderr)
        else:
            print(f"orch: {e}", file=sys.stderr)
        return e.exit_code
    except (FileNotFoundError, PermissionError) as e:
        print(f"orch: {e}", file=sys.stderr)
        return NotFoundError.exit_code
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":
    sys.exit(main())
