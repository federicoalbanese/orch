from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orch import registry
from orch.config import build_config
from orch.paths import resolve_paths
from orch.store import Store

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def all_tools(name: str) -> str:
    return f"/usr/bin/{name}"


def no_tools(name: str) -> None:
    return None


@pytest.fixture
def paths(tmp_path: Path):
    p = resolve_paths(str(tmp_path / "orch-home"))
    p.ensure()
    return p


@pytest.fixture
def store(paths):
    s = Store(paths.db_file)
    yield s
    s.close()


@pytest.fixture
def cfg():
    return build_config()


@pytest.fixture
def make_repo(tmp_path: Path):
    def _make(name: str, files: dict[str, str], git_init: bool = True) -> Path:
        root = tmp_path / "repos" / name
        root.mkdir(parents=True)
        write_files(root, files)
        if git_init:
            git(root, "init", "-q", "-b", "main")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "initial")
        return root

    return _make


PY_REPO = {
    "Makefile": "test:\n\tpython3 -m pytest -q\n\nlint:\n\truff check .\n",
    "pyproject.toml": "[project]\nname = 'demo'\n\n[tool.pytest.ini_options]\n",
    "src/demo/__init__.py": "",
    "tests/test_demo.py": "def test_ok():\n    assert True\n",
    "README.md": "# demo\n",
}


@pytest.fixture
def project(store, paths, make_repo):
    root = make_repo("demo", PY_REPO)
    proj, _ = registry.add_project(store, paths, root, "user", which=all_tools)
    return registry.confirm_commands(store, proj.id, None, "user")


def task_dict(key: str = "T1", **overrides: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "key": key,
        "title": f"Task {key}",
        "objective": "Do the thing",
        "scope": {"allow": ["src/demo/**", "tests/**"]},
        "instructions": "Change the code.",
        "acceptance": [
            {"id": "A1", "text": "tests pass", "verify": {"kind": "command", "command": "test"}},
            {"id": "A2", "text": "code is clean", "verify": {"kind": "review"}},
        ],
        "risk": "low",
    }
    t.update(overrides)
    return t


def plan_dict(project_id: str, tasks: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "schema_version": 1,
        "project_id": project_id,
        "title": "Demo plan",
        "objective": "Improve the demo",
        "tasks": tasks if tasks is not None else [task_dict()],
    }
    p.update(overrides)
    return p
