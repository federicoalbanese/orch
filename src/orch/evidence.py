"""Evidence the engine gathers itself, independently of what the executor claims.

- Snapshots record the full working tree (tracked, modified and untracked files,
  honouring .gitignore) as a git tree object, built through a temporary index.
  The user's index, working tree and branches are never touched.
- The diff between two snapshots is exactly what changed during a run, even when
  the user had unrelated uncommitted work before it started.
- Verification runs only the project's confirmed commands.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orch import globs, gitutil
from orch.config import Config
from orch.errors import OrchError, PolicyError
from orch.models import Project, TaskSpec
from orch.policy import classify_paths, is_protected_branch
from orch.redact import redact, truncate_middle

SNAPSHOT_TIMEOUT = 300.0


@dataclass
class Snapshot:
    head: str | None
    tree: str
    branch: str | None
    dirty: list[str] = field(default_factory=list)


def _git(root: Path, *args: str, env: dict[str, str] | None = None, timeout: float = 60.0) -> str:
    rc, out = gitutil.run_git(root, *args, env=env, timeout=timeout, strip="-z" not in args)
    if rc != 0:
        raise OrchError(f"git {' '.join(args[:3])} failed in {root}: {out[-500:]}")
    return out


def dirty_paths(root: Path) -> list[str]:
    rc, out = gitutil.run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", strip=False)
    if rc != 0:
        return []
    entries = out.split("\0")
    paths, i = [], 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":  # renames carry the old path in the next field
                i += 1
        i += 1
    return sorted(set(paths))


def snapshot(root: Path) -> Snapshot:
    if not gitutil.is_repo(root):
        raise OrchError(f"{root} is not a git repository; evidence collection needs git")
    head = gitutil.head_commit(root)
    with tempfile.TemporaryDirectory(prefix="orch-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if head:
            _git(root, "read-tree", head, env=env, timeout=SNAPSHOT_TIMEOUT)
        _git(root, "add", "-A", "--", ".", env=env, timeout=SNAPSHOT_TIMEOUT)
        tree = _git(root, "write-tree", env=env, timeout=SNAPSHOT_TIMEOUT)
    return Snapshot(head=head, tree=tree, branch=gitutil.current_branch(root), dirty=dirty_paths(root))


@dataclass
class FileChange:
    status: str
    path: str
    old_path: str | None = None
    added: int | None = None
    deleted: int | None = None


def diff_trees(
    root: Path, before: str, after: str, max_patch: int, paths: list[str] | None = None
) -> tuple[list[FileChange], str, bool]:
    """Changes between two trees, optionally limited to `paths` (an empty list means no paths at all)."""
    if paths is not None and not paths:
        return [], "", False
    spec = ["--", *paths] if paths else []
    changes: list[FileChange] = []
    out = _git(root, "diff", "--name-status", "-M", "-z", "--no-color", before, after, *spec)
    parts = [p for p in out.split("\0") if p != ""]
    i = 0
    while i < len(parts):
        status = parts[i]
        if status[0] in "RC":
            changes.append(FileChange(status[0], parts[i + 2], old_path=parts[i + 1]))
            i += 3
        else:
            changes.append(FileChange(status[0], parts[i + 1]))
            i += 2
    stats = _git(root, "diff", "--numstat", "-M", "-z", before, after, *spec)
    by_path = {c.path: c for c in changes}
    fields = stats.split("\0")
    j = 0
    while j < len(fields):
        rec = fields[j]
        if not rec:
            j += 1
            continue
        added, deleted, path = (rec.split("\t", 2) + ["", ""])[:3]
        if path == "":  # rename: numstat puts old and new paths in the following fields
            path = fields[j + 2] if j + 2 < len(fields) else ""
            j += 2
        c = by_path.get(path)
        if c is not None:
            c.added = int(added) if added.isdigit() else None
            c.deleted = int(deleted) if deleted.isdigit() else None
        j += 1
    patch = _git(root, "diff", "-M", "--no-color", "--no-ext-diff", before, after, *spec)
    patch, truncated = truncate_middle(redact(patch), max_patch)
    return changes, patch, truncated


def preflight(
    root: Path,
    project: Project,
    spec: TaskSpec,
    cfg: Config,
    last_after_tree: str | None = None,
    approved: dict[str, str] | None = None,
) -> list[str]:
    """Reasons not to start a run on this working tree right now (empty = go).

    First run of a task: uncommitted changes inside its scope would be attributed to
    the executor, so they block, unless they are exactly what an APPROVED task left
    behind (`approved`: path -> tree with the approved content). That lets dependent
    tasks build on approved, not-yet-committed work, while any other edit still blocks.
    Later runs (corrections, retries): the executor's own earlier edits are expected;
    only in-scope files changed since its last run block.
    """
    problems: list[str] = []
    if not gitutil.is_repo(root):
        return [f"{root} is not a git repository"]
    branch = gitutil.current_branch(root)
    if branch is None:
        problems.append("HEAD is detached; check out a working branch first")
    elif is_protected_branch(branch, cfg) and cfg.requires_approval("git_branch"):
        problems.append(f"on protected branch {branch!r}; switch to a working branch first")

    def in_scope(paths: list[str]) -> list[str]:
        return [p for p in paths if globs.match_any(p, spec.scope.allow) and not globs.match_any(p, spec.scope.deny)]

    if last_after_tree is None:
        touched = in_scope(dirty_paths(root))
        if touched and approved:
            touched = _unaccounted(root, touched, approved)
        what = "uncommitted changes inside the task's scope would mix with the executor's work"
    else:
        changes, _, _ = diff_trees(root, last_after_tree, snapshot(root).tree, 1000)
        touched = in_scope(sorted({c.path for c in changes}))
        what = "files in the task's scope changed since the executor's last run and would be attributed to it"
    if touched:
        problems.append(f"{what}: " + ", ".join(touched[:10]) + (" ..." if len(touched) > 10 else ""))
    return problems


def _unaccounted(root: Path, paths: list[str], approved: dict[str, str]) -> list[str]:
    """The paths whose current content is not exactly what an approved task produced."""
    current = snapshot(root).tree
    by_tree: dict[str, list[str]] = {}
    unaccounted = [p for p in paths if p not in approved]
    for p in paths:
        if p in approved:
            by_tree.setdefault(approved[p], []).append(p)
    for tree, group in by_tree.items():
        differs = _git(root, "diff", "--name-only", "--no-renames", tree, current, "--", *group).splitlines()
        unaccounted += [p for p in group if p in differs]
    return sorted(unaccounted)


def ensure_working_branch(root: Path, branch_name: str, cfg: Config) -> str | None:
    """If HEAD is on a protected branch and creating branches is automatic, switch to a new branch.

    Uses `git switch -c`, which keeps uncommitted changes and never discards anything.
    Returns the new branch name, or None if nothing was needed.
    """
    current = gitutil.current_branch(root)
    if current is None or not is_protected_branch(current, cfg) or cfg.requires_approval("git_branch"):
        return None
    rc, _ = gitutil.run_git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch_name}")
    if rc == 0:
        raise PolicyError(f"branch {branch_name!r} already exists; switch to it or choose another")
    _git(root, "switch", "-c", branch_name)
    return branch_name


def collect(
    root: Path,
    baseline: str,
    before: Snapshot,
    spec: TaskSpec,
    cfg: Config,
    report: dict[str, Any] | None,
    prior_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Compare the tree after a run with the task's baseline (cumulative) and the run's start (this attempt).

    Only paths the executor touched during one of this task's attempts are attributed
    to it (`prior_paths` holds those of earlier attempts). Files created between
    attempts, such as artifacts of the engine's own check runs, are not.
    """
    after = snapshot(root)
    max_patch = cfg.get("limits", "evidence_output_bytes") * 3
    attempt_changes, _, _ = diff_trees(root, before.tree, after.tree, 1000)
    attempt_paths = {c.path for c in attempt_changes} | {c.old_path for c in attempt_changes if c.old_path}
    executor_paths = sorted(set(prior_paths or ()) | attempt_paths)
    changes, patch, truncated = diff_trees(root, baseline, after.tree, max_patch, executor_paths)
    changed = sorted({c.path for c in changes} | {c.old_path for c in changes if c.old_path})
    violations = [p for p in changed
                  if not globs.match_any(p, spec.scope.allow) or globs.match_any(p, spec.scope.deny)]
    classes = classify_paths(changed, cfg)
    deleted_files = sorted(c.path for c in changes if c.status == "D")
    deleted_lines = sum(c.deleted or 0 for c in changes)

    flags: list[dict[str, str]] = []

    def flag(action: str, detail: str) -> None:
        if cfg.requires_approval(action):
            flags.append({"action": action, "detail": detail})

    if classes.sensitive:
        flag("sensitive_file", ", ".join(classes.sensitive))
    if classes.dependency:
        flag("dependency_change", ", ".join(classes.dependency))
    if classes.migration:
        flag("migration", ", ".join(classes.migration))
    if deleted_files:
        flag("delete_files", ", ".join(deleted_files))
    if deleted_lines > cfg.get("policy", "large_deletion_lines"):
        flag("large_deletion", f"{deleted_lines} lines deleted")

    claimed = sorted({normalize_claimed(p, root) for p in (report or {}).get("files_changed", []) or []})
    return {
        "baseline_tree": baseline,
        "before": asdict(before),
        "after": asdict(after),
        "changes": [asdict(c) for c in changes],
        "attempt_changed_files": sorted(attempt_paths),
        "changed_files": changed,
        "lines_added": sum(c.added or 0 for c in changes),
        "lines_deleted": deleted_lines,
        "patch": patch,
        "patch_truncated": truncated,
        "scope_violations": violations,
        "policy_flags": flags,
        "report": report,
        "discrepancies": {
            "claimed_not_changed": sorted(set(claimed) - set(changed)),
            "changed_not_claimed": sorted(set(changed) - set(claimed)) if report is not None else [],
        },
    }


def normalize_claimed(path: str, root: Path) -> str:
    p = path.removeprefix("file://")
    root_s = str(root.resolve())
    if p.startswith(root_s + os.sep):
        p = p[len(root_s) + 1:]
    return globs.normalize_path(p)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, PermissionError):
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_checks(
    root: Path,
    project: Project,
    names: list[str],
    max_output: int,
    cancel: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Run confirmed project commands and capture their real results."""
    shell = shutil.which("bash") or "/bin/sh"
    results: list[dict[str, Any]] = []
    for name in names:
        cmd = project.commands.get(name)
        if cmd is None or not cmd.confirmed:
            results.append({"name": name, "run": cmd.run if cmd else None, "skipped": True, "passed": False,
                            "reason": "command missing" if cmd is None else "command not confirmed"})
            continue
        if cancel is not None and cancel.is_set():
            results.append({"name": name, "run": cmd.run, "skipped": True, "passed": False, "reason": "cancelled"})
            continue
        started = time.monotonic()
        timed_out = False
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(
                [shell, "-c", cmd.run], cwd=root / cmd.cwd, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
            deadline = started + cmd.timeout_seconds
            while proc.poll() is None:
                if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                    timed_out = time.monotonic() > deadline
                    _kill_group(proc)
                    break
                time.sleep(0.1)
            out.seek(0)
            text = out.read().decode("utf-8", errors="replace")
        output, truncated = truncate_middle(redact(text), max_output)
        results.append({
            "name": name, "run": cmd.run, "skipped": False,
            "exit_code": proc.returncode, "passed": proc.returncode == 0 and not timed_out,
            "timed_out": timed_out, "duration_seconds": round(time.monotonic() - started, 2),
            "output": output, "output_truncated": truncated,
        })
    return results


def check_names(spec: TaskSpec) -> list[str]:
    names = list(spec.required_checks)
    for a in spec.acceptance:
        if a.verify.kind == "command" and a.verify.command and a.verify.command not in names:
            names.append(a.verify.command)
    return names
