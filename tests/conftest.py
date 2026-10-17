from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orch import plans, registry, workflow
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


@pytest.fixture(autouse=True)
def _like_ci(monkeypatch):
    """Behave like a fresh CI runner: Python writes bytecode next to the code it imports.

    Git never reads the user's own configuration (hooks, signing, identity), so commits
    made by the code under test behave the same on every machine.
    """
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, GIT_ENV[key])


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


# -- a small repository with a bug and a confirmed check ----------------------

CALC = "def add(a, b):\n    return a - b\n"
GOOD = "def add(a, b):\n    return a + b\n"
DONE = {"status": "DONE", "files_changed": ["calc.py"], "summary": "fixed add", "unresolved": []}
CALC_CHECK = "python3 -c 'import calc; assert calc.add(2, 3) == 5'"


@pytest.fixture
def calc(store, paths, make_repo):
    """A tiny repo with a bug, a confirmed check, opted in, on a working branch."""
    root = make_repo("calc", {"calc.py": CALC, "pyproject.toml": "[project]\nname='calc'\n", "README.md": "# calc\n"})
    git(root, "switch", "-q", "-c", "work")
    p, _ = registry.add_project(store, paths, root, "user", which=all_tools)
    registry.set_command(store, p.id, "test", CALC_CHECK, "user", timeout_seconds=60, confirm=True)
    registry.remove_command(store, p.id, "lint", "user") if "lint" in store.get_project(p.id).commands else None
    registry.set_engine(store, p.id, True, "user")
    registry.set_field(store, p.id, "executor", "fake", "user")
    return store.get_project(p.id)


def calc_task(key="T1", **kw):
    t = task_dict(key, scope={"allow": ["calc.py", "test_calc.py"]}, relevant_files=["calc.py"],
                  acceptance=[{"id": "A1", "text": "add() adds", "verify": {"kind": "command", "command": "test"}},
                              {"id": "A2", "text": "a regression test exists", "verify": {"kind": "review"}}])
    t.update(kw)
    return t


def import_tasks(store, cfg, project, *tasks):
    return [t.id for t in plans.import_plan(store, cfg, plan_dict(project.id, list(tasks)), "claude").tasks]


def engine(store, paths, cfg, ex, **kw):
    return workflow.Engine(store, paths, cfg, executor_factory=lambda name: ex, sleep=lambda s: None,
                           cancel_poll_seconds=0.05, **kw)


def review_doc(decision, met=True, findings=None, summary="looks right"):
    return {"decision": decision, "summary": summary,
            "criteria": [{"id": "A1", "met": met, "evidence": "test passed"}, {"id": "A2", "met": met}],
            "findings": findings or []}


# -- a stand-in agy binary (tests/fake_agy.py) ---------------------------------


@pytest.fixture
def fake_agy_bin(tmp_path: Path) -> Path:
    script = tmp_path / "bin" / "agy"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).parent / 'fake_agy.py'} \"$@\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def agy_result(**result: Any) -> str:
    """One `result` line of agy's stream-json output."""
    base = {"conversation_id": "c-1", "status": "SUCCESS", "response": "", "duration_seconds": 1, "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": 2}}
    base.update(result)
    return json.dumps({"event": "result", "result": base})
