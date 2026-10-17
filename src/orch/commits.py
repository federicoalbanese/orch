"""Commits for approved tasks. Creating one is policy action `git_commit`, which needs the user by default.

- **Exactly the task's files.** orch commits the files its evidence attributes to the
  executor (`changed_files`). It never commits check artifacts, other uncommitted work,
  or anything already staged in the user's index.
- **Only what was reviewed.** Each file must still hold the content it had when the task was
  approved. A file that a later approved task changed again needs that task in the same commit.
- **Never pushes.** Pushing stays with the user.
- **No attribution trailers.** The message is built from the task and its evidence. orch never
  adds `Co-Authored-By` trailers and removes any that appear in a message it is given.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orch import evidence, gitutil
from orch.config import Config
from orch.errors import OrchError, PolicyError, ValidationError
from orch.models import Task, TaskStatus
from orch.policy import is_protected_branch
from orch.registry import effective_config, require_user
from orch.store import Store

SUBJECT_MAX = 72
WIDTH = 72
HOOK_TIMEOUT = 600.0
_ATTRIBUTION = re.compile(r"^\s*co-authored-by\s*:", re.I)
_ENV = {**gitutil._GIT_ENV, "GIT_LITERAL_PATHSPECS": "1"}


@dataclass
class CommitPlan:
    project_id: str
    root: str
    branch: str | None
    tasks: list[str]
    files: list[str]                 # attributed files whose approved content differs from HEAD
    already_committed: list[str]     # attributed files that already match HEAD
    message: str
    committed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git(root: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout, env={**os.environ, **_ENV})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OrchError(f"git {args[0]} failed in {root}: {e}") from e


def _git_ok(root: Path, *args: str, timeout: float = 60.0) -> str:
    proc = _git(root, *args, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[-800:]
        raise OrchError(f"git {args[0]} failed in {root}: {detail}")
    return proc.stdout


def _differing(root: Path, old: str, new: str, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    out = _git_ok(root, "diff", "--name-only", "-z", "--no-renames", old, new, "--", *paths)
    return {p for p in out.split("\0") if p}


def strip_attribution(message: str) -> str:
    lines = [line for line in message.splitlines() if not _ATTRIBUTION.match(line)]
    return "\n".join(lines).strip() + "\n"


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def build_message(tasks: list[Task], evidences: dict[str, dict[str, Any]], plan_titles: dict[int, str]) -> str:
    """A commit message from the tasks' specs and the engine's own evidence (never the executor's claims)."""
    if len(tasks) == 1:
        subject = tasks[0].spec.title
    elif len({t.plan_id for t in tasks}) == 1:
        subject = plan_titles[tasks[0].plan_id]
    else:
        subject = "; ".join(t.spec.title for t in tasks)
    body: list[str] = []
    for t in tasks:
        ev = evidences[t.id]
        checks = {c["name"]: c for c in ev.get("checks", [])}
        if len(tasks) > 1:
            body.append(_one_line(t.spec.title, WIDTH))
        body += [textwrap.fill(" ".join(t.spec.objective.split()), WIDTH), "", "Acceptance criteria:"]
        for a in t.spec.acceptance:
            how = "reviewed"
            if a.verify.kind == "command" and a.verify.command:
                c = checks.get(a.verify.command, {})
                how = f"`{a.verify.command}` " + ("passed" if c.get("passed") else "did not pass")
            body.append(textwrap.fill(f"- {a.id}: {' '.join(a.text.split())} ({how})", WIDTH, subsequent_indent="  "))
        if checks:
            verdicts = ", ".join(f"{n} {'passed' if c.get('passed') else 'skipped' if c.get('skipped') else 'FAILED'}"
                                 for n, c in checks.items())
            body.append(textwrap.fill(f"Checks run by orch: {verdicts}.", WIDTH))
        body.append("")
    trailers = [f"Orch-Task: {t.id}" for t in tasks]
    lines = [_one_line(subject, SUBJECT_MAX), "", *body, *trailers]
    return strip_attribution("\n".join(lines))


def _load(store: Store, cfg: Config, task_ids: list[str]) -> tuple[list[Task], Any, Config]:
    if not task_ids:
        raise ValidationError("name at least one task")
    tasks = [store.get_task(t) for t in dict.fromkeys(task_ids)]
    projects = {t.project_id for t in tasks}
    if len(projects) > 1:
        raise ValidationError(f"tasks belong to different projects ({', '.join(sorted(projects))}); commit each separately")
    project = store.get_project(tasks[0].project_id)
    return tasks, project, effective_config(cfg, project)


def prepare(store: Store, cfg: Config, task_ids: list[str], message: str | None = None) -> CommitPlan:
    """Everything a commit would contain, checked against the evidence. Changes nothing."""
    tasks, project, pcfg = _load(store, cfg, task_ids)
    root = Path(project.root)
    selected = {t.id for t in tasks}
    problems: list[str] = []
    branch = gitutil.current_branch(root)
    if branch is None:
        problems.append("HEAD is detached; check out a working branch first")
    elif is_protected_branch(branch, pcfg):
        problems.append(f"on protected branch {branch!r}; commit on a working branch (orch creates orch/P<n>)")

    evidences: dict[str, dict[str, Any]] = {}
    for t in tasks:
        latest = store.latest_evidence(t.id)
        if t.status != TaskStatus.APPROVED:
            problems.append(f"{t.id} is {t.status.value}; only APPROVED tasks can be committed")
        elif latest is None:
            problems.append(f"{t.id} has no evidence, so orch does not know which files are its own")
        else:
            evidences[t.id] = latest["data"]
    if problems:
        raise PolicyError("cannot commit:\n" + "\n".join(f"  - {p}" for p in problems))

    owners = store.approved_files(project.id)
    files = sorted({p for ev in evidences.values() for p in ev.get("changed_files", [])})
    expected: dict[str, list[str]] = {}
    for path in files:
        owner, tree = owners[path]
        if owner not in selected:
            mine = next(t.id for t in tasks if path in evidences[t.id].get("changed_files", []))
            problems.append(f"{path} was changed again by {owner} after {mine}; commit them together "
                            f"(orch task commit {' '.join([*(t.id for t in tasks), owner])})")
        else:
            expected.setdefault(tree, []).append(path)
    current = evidence.snapshot(root).tree
    changed_since = sorted({p for tree, group in expected.items() for p in _differing(root, tree, current, group)})
    if changed_since:
        problems.append("changed since the review approved them (orch only commits reviewed content): "
                        + ", ".join(changed_since))
    if problems:
        raise PolicyError("cannot commit:\n" + "\n".join(f"  - {p}" for p in problems))

    head = gitutil.head_commit(root)
    to_commit = sorted(_differing(root, head, current, files)) if head else files
    if not to_commit:
        raise PolicyError(f"nothing to commit: the files of {', '.join(sorted(selected))} already match HEAD")
    if message is not None:
        message = strip_attribution(message)
        if not message.strip():
            raise ValidationError("the commit message is empty")
    else:
        titles = {t.plan_id: store.get_plan(t.plan_id)["title"] for t in tasks}
        message = build_message(tasks, evidences, titles)
    return CommitPlan(project_id=project.id, root=str(root), branch=branch, tasks=[t.id for t in tasks],
                      files=to_commit, already_committed=sorted(set(files) - set(to_commit)), message=message)


def create(store: Store, cfg: Config, task_ids: list[str], actor: str, message: str | None = None) -> tuple[CommitPlan, str]:
    """Commit the approved tasks' files on the current branch. Returns the plan and the new commit id."""
    tasks, project, pcfg = _load(store, cfg, task_ids)
    if pcfg.requires_approval("git_commit"):
        require_user(actor, "committing (policy action git_commit requires approval)")
    plan = prepare(store, cfg, task_ids, message)
    root = Path(plan.root)
    tracked = {p for p in _git_ok(root, "ls-files", "-z", "--", *plan.files).split("\0") if p}
    head = gitutil.head_commit(root)
    in_head = ({p for p in _git_ok(root, "ls-tree", "-r", "-z", "--name-only", head, "--", *plan.files).split("\0") if p}
               if head else set())
    new = [p for p in plan.files if p not in tracked and p not in in_head]
    if new:  # make new files known to git without staging anything else
        _git_ok(root, "add", "--intent-to-add", "--", *new)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", prefix="orch-commit-", delete=False) as f:
        f.write(plan.message)
    try:
        # --only: commit these paths as they are in the working tree, and leave the rest of the index alone.
        proc = _git(root, "commit", "--only", "--cleanup=whitespace", "-F", f.name, "--", *plan.files,
                    timeout=HOOK_TIMEOUT)
    finally:
        os.unlink(f.name)
    if proc.returncode != 0:
        if new:
            _git(root, "reset", "-q", "--", *new)
        detail = (proc.stderr.strip() or proc.stdout.strip())[-800:]
        raise OrchError(f"git commit failed (nothing was committed): {detail}")
    sha = _git_ok(root, "rev-parse", "HEAD").strip()
    shown = _git_ok(root, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", "--root", sha)
    plan.committed_files = sorted(p for p in shown.split("\0") if p)
    with store.tx():
        for t in tasks:
            store.add_event("git.committed", "task", t.id, actor, project.id,
                            {"commit": sha, "branch": plan.branch, "files": plan.committed_files, "tasks": plan.tasks})
    return plan, sha
