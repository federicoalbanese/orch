"""Layered, project-isolated context with provenance.

Layers, in priority order: global framework rules, the task, the project's
context.md, the project's knowledge entries, and the task's history. Only the
current project's knowledge is ever loaded; shared entries are opt-in.

Every knowledge entry records its source and the commit it was verified at.
`check_staleness` marks an entry stale once any file it refers to has changed
since then, so outdated facts stop reaching prompts until someone re-verifies them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from orch import gitutil, globs
from orch.errors import ValidationError
from orch.models import Project, Task
from orch.paths import Paths
from orch.store import Store

KINDS = (
    "decision", "constraint", "convention", "architecture", "limitation",
    "known_issue", "resolution", "debt", "note",
)
_KIND_ORDER = {k: i for i, k in enumerate(KINDS)}


def global_rules() -> str:
    return resources.files("orch").joinpath("resources/executor_rules.md").read_text(encoding="utf-8")


def add_entry(
    store: Store,
    project: Project | None,
    kind: str,
    title: str,
    body: str,
    source: str,
    actor: str,
    refs: list[str] | None = None,
) -> int:
    errors = []
    if kind not in KINDS:
        errors.append(f"kind must be one of {', '.join(KINDS)}")
    if not title.strip() or not body.strip():
        errors.append("title and body are required")
    if not source.strip():
        errors.append("source is required (a file, URL, review id, or 'user')")
    for r in refs or []:
        problem = globs.validate_pattern(r)
        if problem:
            errors.append(f"ref {problem}")
    if errors:
        raise ValidationError("invalid context entry", errors)
    commit = gitutil.head_commit(Path(project.root)) if project else None
    return store.add_context_entry(
        project.id if project else None, kind, title.strip(), body.strip(), source.strip(), actor,
        refs=refs or [], source_commit=commit,
    )


def check_staleness(store: Store, project: Project, actor: str) -> list[dict[str, object]]:
    """Mark active entries stale when files they refer to changed since they were verified."""
    root = Path(project.root)
    results: list[dict[str, object]] = []
    for e in store.list_context_entries(project.id, statuses=["active"]):
        if not e["refs"]:
            continue
        if not e["source_commit"]:
            results.append({"id": e["id"], "title": e["title"], "result": "unverifiable (no commit recorded)"})
            continue
        changed = gitutil.changed_since(root, e["source_commit"], e["refs"])
        if changed is None:
            reason = f"verification commit {e['source_commit'][:12]} no longer exists"
            store.set_context_status(e["id"], "stale", actor, reason)
            results.append({"id": e["id"], "title": e["title"], "result": "stale", "reason": reason})
        elif changed:
            reason = "referenced files changed: " + ", ".join(changed[:10])
            store.set_context_status(e["id"], "stale", actor, reason)
            results.append({"id": e["id"], "title": e["title"], "result": "stale", "changed": changed})
        else:
            results.append({"id": e["id"], "title": e["title"], "result": "fresh"})
    return results


def reverify(store: Store, project: Project, entry_id: int, actor: str) -> None:
    commit = gitutil.head_commit(Path(project.root)) or ""
    store.set_context_status(entry_id, "active", actor, "re-verified", reverified_at_commit=commit)


@dataclass
class Layer:
    name: str
    title: str
    content: str
    truncated: bool = False


@dataclass
class ContextBundle:
    layers: list[Layer]
    budget: int
    omitted: list[str] = field(default_factory=list)

    @property
    def used(self) -> int:
        return sum(len(layer.content) for layer in self.layers)

    def render(self) -> str:
        parts = [f"## {layer.title}\n\n{layer.content.strip()}\n" for layer in self.layers]
        if self.omitted:
            parts.append("## Omitted (context budget)\n\n" + "\n".join(f"- {o}" for o in self.omitted) + "\n")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "budget": self.budget,
            "used": self.used,
            "omitted": self.omitted,
            "layers": [vars(layer) for layer in self.layers],
        }


def render_task(task: Task) -> str:
    s = task.spec
    lines = [
        f"Task {task.id}: {s.title}",
        "",
        f"Objective: {s.objective}",
        f"Risk: {s.risk.value}",
        "",
        "Allowed scope (you may change only these paths):",
        *[f"- {p}" for p in s.scope.allow],
    ]
    if s.scope.deny:
        lines += ["", "Never change:", *[f"- {p}" for p in s.scope.deny]]
    if s.relevant_files:
        lines += ["", "Relevant files:", *[f"- {p}" for p in s.relevant_files]]
    lines += ["", "Instructions:", s.instructions.strip(), "", "Acceptance criteria:"]
    for a in s.acceptance:
        how = f"verified by running `{a.verify.command}`" if a.verify.kind == "command" else "verified in review"
        lines.append(f"- [{a.id}] {a.text} ({how})")
    if s.required_checks:
        lines += ["", "Checks the engine will run: " + ", ".join(s.required_checks)]
    if s.deliverables:
        lines += ["", "Deliverables:", *[f"- {d}" for d in s.deliverables]]
    return "\n".join(lines)


def build_context(
    store: Store,
    paths: Paths,
    project: Project,
    budget: int,
    task: Task | None = None,
    history: list[str] | None = None,
    include_shared: bool = False,
) -> ContextBundle:
    mandatory = [Layer("global", "Framework rules", global_rules())]
    if task is not None:
        if task.project_id != project.id:
            raise ValidationError(f"task {task.id} belongs to {task.project_id}, not {project.id}")
        mandatory.append(Layer("task", "Task", render_task(task)))

    optional: list[Layer] = []
    ctx_file = paths.project_dir(project.id) / "context.md"
    if ctx_file.exists():
        optional.append(Layer("project", f"Project context: {project.name}", ctx_file.read_text(encoding="utf-8")))
    entries = store.list_context_entries(project.id, statuses=["active"])
    if include_shared:
        entries += store.list_context_entries(None, statuses=["active"])
    entries.sort(key=lambda e: (_KIND_ORDER.get(e["kind"], 99), e["id"]))
    if entries:
        body = "\n\n".join(
            f"[{e['kind']} #{e['id']}] {e['title']}\n{e['body']}\n(source: {e['source']}, verified {e['verified_at'][:10]})"
            for e in entries
        )
        optional.append(Layer("knowledge", "Project knowledge", body))
    if history:
        optional.append(Layer("history", "History of this task", "\n\n".join(history)))

    bundle = ContextBundle(layers=list(mandatory), budget=budget)
    remaining = budget - bundle.used
    stale = store.list_context_entries(project.id, statuses=["stale"])
    bundle.omitted += [f"stale knowledge #{e['id']}: {e['title']}" for e in stale]
    for layer in optional:
        if remaining <= 0:
            bundle.omitted.append(f"{layer.title} ({len(layer.content)} chars)")
            continue
        if len(layer.content) > remaining:
            marker = "\n\n[... truncated to fit the context budget ...]"
            keep = max(0, remaining - len(marker))
            layer = Layer(layer.name, layer.title, layer.content[:keep] + marker, truncated=True)
        bundle.layers.append(layer)
        remaining = budget - bundle.used
    return bundle
