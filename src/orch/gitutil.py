"""Read-only git queries. Nothing in this module changes a repository."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def run_git(
    root: Path, *args: str, timeout: float = 15.0, env: dict[str, str] | None = None, strip: bool = True
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **_GIT_ENV, **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, proc.stdout.strip() if strip else proc.stdout


def is_repo(root: Path) -> bool:
    rc, out = run_git(root, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def toplevel(root: Path) -> Path | None:
    rc, out = run_git(root, "rev-parse", "--show-toplevel")
    return Path(out) if rc == 0 and out else None


def head_commit(root: Path) -> str | None:
    rc, out = run_git(root, "rev-parse", "--verify", "-q", "HEAD")
    return out if rc == 0 and out else None


def current_branch(root: Path) -> str | None:
    rc, out = run_git(root, "symbolic-ref", "--short", "-q", "HEAD")
    return out if rc == 0 and out else None


def remote_url(root: Path, name: str = "origin") -> str | None:
    rc, out = run_git(root, "remote", "get-url", name)
    if rc != 0 or not out:
        rc, names = run_git(root, "remote")
        first = names.splitlines()[0] if rc == 0 and names else None
        if not first:
            return None
        rc, out = run_git(root, "remote", "get-url", first)
    return sanitize_remote(out) if rc == 0 and out else None


def sanitize_remote(url: str) -> str:
    """Drop credentials from http(s) remotes (https://user:token@host/... -> https://host/...)."""
    return re.sub(r"^(https?://)[^/@]+@", r"\1", url.strip())


def default_branch(root: Path) -> str | None:
    rc, out = run_git(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if rc == 0 and out:
        return out.split("/", 1)[1] if "/" in out else out
    rc, out = run_git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin")
    names = {n.removeprefix("origin/") for n in out.splitlines()} if rc == 0 else set()
    for candidate in ("main", "master", "develop", "dev"):
        if candidate in names:
            return candidate
    return current_branch(root)


def is_dirty(root: Path) -> bool | None:
    rc, out = run_git(root, "status", "--porcelain", "--untracked-files=normal")
    return None if rc != 0 else bool(out)


def changed_since(root: Path, commit: str, paths: list[str]) -> list[str] | None:
    """Files under `paths` that differ between `commit` and the working tree; None if commit is unknown."""
    rc, _ = run_git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    if rc != 0:
        return None
    rc, out = run_git(root, "diff", "--name-only", commit, "--", *paths)
    if rc != 0:
        return None
    rc2, untracked = run_git(root, "ls-files", "--others", "--exclude-standard", "--", *paths)
    files = set(out.splitlines()) | (set(untracked.splitlines()) if rc2 == 0 else set())
    return sorted(f for f in files if f)
