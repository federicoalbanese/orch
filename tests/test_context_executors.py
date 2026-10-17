import threading
from pathlib import Path

import pytest

from orch import context, executors, plans
from orch.errors import OrchError, ValidationError
from orch.executors.base import ExecRequest, ExecStatus, Mode
from orch.executors.fake import FakeExecutor, FakeStep

from conftest import git, plan_dict


def test_context_entries_go_stale_when_referenced_files_change(store, project):
    root = Path(project.root)
    eid = context.add_entry(store, project, "convention", "Tests live in tests/", "pytest, one file per module",
                            "README.md", "claude", refs=["tests/"])
    other = context.add_entry(store, project, "decision", "Use make", "make is the entrypoint", "user", "user",
                              refs=["Makefile"])
    entry = store.list_context_entries(project.id)[0]
    assert entry["source_commit"] == git(root, "rev-parse", "HEAD")

    (root / "tests" / "test_new.py").write_text("def test_x(): pass\n")
    results = {r["id"]: r for r in context.check_staleness(store, project, "engine")}
    assert results[eid]["result"] == "stale" and results[eid]["changed"] == ["tests/test_new.py"]
    assert results[other]["result"] == "fresh"
    assert [e["id"] for e in store.list_context_entries(project.id, statuses=["stale"])] == [eid]

    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "more tests")
    context.reverify(store, project, eid, "user")
    assert context.check_staleness(store, project, "engine")[0]["result"] == "fresh"


def test_context_entry_validation(store, project):
    with pytest.raises(ValidationError):
        context.add_entry(store, project, "rumour", "t", "b", "s", "claude")
    with pytest.raises(ValidationError):
        context.add_entry(store, project, "note", "t", "b", "s", "claude", refs=["../etc"])


def test_build_context_layers_budget_and_isolation(store, paths, cfg, project, make_repo):
    from orch import registry
    from conftest import all_tools
    other_root = make_repo("other", {"go.mod": "module o\n"})
    other, _ = registry.add_project(store, paths, other_root, "user", which=all_tools)
    context.add_entry(store, other, "decision", "OTHER-SECRET", "must not leak", "user", "user")
    context.add_entry(store, project, "decision", "Use make", "make is the entrypoint", "user", "user")
    task = plans.import_plan(store, cfg, plan_dict(project.id), "claude").tasks[0]

    bundle = context.build_context(store, paths, project, 100_000, task)
    text = bundle.render()
    assert [l.name for l in bundle.layers] == ["global", "task", "project", "knowledge"]
    assert "Use make" in text and "OTHER-SECRET" not in text
    assert "Allowed scope" in text and "[A1] tests pass" in text

    small = context.build_context(store, paths, project, len(context.global_rules()) + 50, task)
    assert [l.name for l in small.layers] == ["global", "task"]
    assert small.omitted and "Project context" in small.omitted[0]

    with pytest.raises(ValidationError):
        context.build_context(store, paths, other, 10_000, task)


def test_fake_executor_edits_and_resumes(tmp_path):
    ex = FakeExecutor([
        FakeStep(files={"a.txt": "hello", "sub/b.txt": "x"}, report={"status": "DONE"}),
        FakeStep(files={"a.txt": None}),
    ])
    events = []
    r1 = ex.run(ExecRequest("T", 1, str(tmp_path), "do it"), on_event=events.append)
    assert r1.status == ExecStatus.SUCCEEDED and (tmp_path / "sub/b.txt").read_text() == "x"
    r2 = ex.run(ExecRequest("T", 2, str(tmp_path), "fix", resume_conversation_id=r1.conversation_id))
    assert r2.conversation_id == r1.conversation_id and not (tmp_path / "a.txt").exists()
    assert events[0]["event"] == "init" and events[-1]["event"] == "result"


def test_fake_executor_safety_cancel_and_timeout(tmp_path):
    escape = FakeExecutor([FakeStep(files={"../evil.txt": "x"})]).run(ExecRequest("T", 1, str(tmp_path), "p"))
    assert escape.status == ExecStatus.FAILED and not (tmp_path.parent / "evil.txt").exists()

    ro = FakeExecutor([FakeStep(files={"a": "x"})]).run(ExecRequest("T", 1, str(tmp_path), "p", mode=Mode.READ_ONLY))
    assert ro.status == ExecStatus.DENIED and not (tmp_path / "a").exists()

    cancel = threading.Event()
    cancel.set()
    r = FakeExecutor([FakeStep(delay=5)]).run(ExecRequest("T", 1, str(tmp_path), "p"), cancel=cancel)
    assert r.status == ExecStatus.CANCELLED

    r = FakeExecutor([FakeStep(delay=5)]).run(ExecRequest("T", 1, str(tmp_path), "p", timeout_seconds=0))
    assert r.status == ExecStatus.TIMED_OUT


def test_executor_registry():
    assert executors.available() == ["agy", "fake", "manual"]
    assert executors.get_executor("fake").capabilities().sends_code_off_machine is False
    assert executors.get_executor("agy").capabilities().sends_code_off_machine is True
    assert executors.get_executor("manual").capabilities().sends_code_off_machine is True
    with pytest.raises(OrchError, match="unknown executor"):
        executors.get_executor("nope")
