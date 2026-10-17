import dataclasses
import json
import threading
import time
from pathlib import Path

import pytest

from orch import evidence, plans, policy, registry, review, workflow
from orch.errors import PolicyError, ValidationError
from orch.executors.base import Classification, ExecStatus
from orch.executors.fake import FakeExecutor, FakeStep
from orch.models import ReviewDecision, TaskStatus
from orch.redact import redact, truncate_middle
from orch.store import Store

from conftest import CALC, DONE, GOOD, all_tools, calc_task, engine, git, import_tasks, review_doc


def test_full_loop_with_a_correction_cycle(store, paths, cfg, calc):
    t1, t2 = import_tasks(store, cfg, calc, calc_task("T1"), calc_task("T2", depends_on=["T1"]))
    ex = FakeExecutor({t1: [
        FakeStep(files={"calc.py": GOOD}, report=DONE, usage={"input_tokens": 100}),
        FakeStep(files={"test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
                 report={**DONE, "files_changed": ["test_calc.py"]}),
    ]})
    eng = engine(store, paths, cfg, ex)

    out = eng.dispatch(t1)
    assert out.status == "IN_REVIEW" and out.checks[0]["passed"]
    first_prompt = ex.requests[0].prompt
    assert "Framework rules" in first_prompt and "Allowed scope" in first_prompt and "engine itself runs: test" in first_prompt

    bundle = review.build_bundle(store, cfg, t1)
    ev = bundle["evidence"]
    assert ev["changed_files"] == ["calc.py"] and ev["scope_violations"] == [] and bundle["approval_blockers"] == []
    assert "-    return a - b" in ev["patch"] and "+    return a + b" in ev["patch"]
    assert "Checks run by the engine" in review.render_bundle(bundle)
    assert "This attempt's diff" not in review.render_bundle(bundle)  # the first attempt is the whole diff

    with pytest.raises(PolicyError, match="criterion A2 is marked unmet"):
        review.decide(store, cfg, t1, review_doc("APPROVED", met=False), "claude")
    finding = {"severity": "major", "category": "tests", "message": "no regression test",
               "instruction": "Add test_calc.py with a pytest test for add(2, 3) == 5."}
    task, _ = review.decide(store, cfg, t1, {**review_doc("NEEDS_CORRECTION"), "criteria": [
        {"id": "A1", "met": True}, {"id": "A2", "met": False}], "findings": [finding]}, "claude")
    assert task.status == TaskStatus.READY and task.review_status == ReviewDecision.NEEDS_CORRECTION
    assert [t.id for t in store.ready_tasks()] == [t1]  # T2 still waits for T1's approval

    out = eng.dispatch(t1)
    assert out.status == "IN_REVIEW"
    correction = ex.requests[1]
    assert correction.resume_conversation_id == "fake-1"
    assert "The original task (still fully in force)" in correction.prompt and "Add test_calc.py" in correction.prompt
    assert "[A1] add() adds" in correction.prompt and "Allowed scope" in correction.prompt
    ev = review.build_bundle(store, cfg, t1)["evidence"]
    assert ev["changed_files"] == ["calc.py", "test_calc.py"]  # cumulative since the task started
    assert ev["attempt_changed_files"] == ["test_calc.py"]
    # The correction's own diff, so a reviewer can see what this attempt changed (found reviewing a real correction)
    assert "+def test_add():" in ev["attempt_patch"] and "return a + b" not in ev["attempt_patch"]
    rendered = review.render_bundle(review.build_bundle(store, cfg, t1))
    assert "## This attempt's diff (correction" in rendered
    assert rendered.index("## This attempt's diff") < rendered.index("## Diff since the task started")

    task, _ = review.decide(store, cfg, t1, review_doc("APPROVED"), "claude")
    assert task.status == TaskStatus.APPROVED and task.review_status == ReviewDecision.APPROVED
    assert [t.id for t in store.ready_tasks()] == [t2]
    kinds = [r["kind"] for r in store.list_runs(t1)]
    assert kinds == ["execute", "correction"]
    assert review.build_bundle(store, cfg, t1)["usage_total"] == {"input_tokens": 100}


def test_auto_fix_loop_runs_without_review(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(files={"calc.py": "def add(a, b):\n    return a * b * 1\n"}, report=DONE),
                       FakeStep(files={"calc.py": GOOD}, report=DONE)])
    out = engine(store, paths, cfg, ex).dispatch(tid)
    assert out.status == "IN_REVIEW" and out.checks[0]["passed"]
    assert [r["kind"] for r in store.list_runs(tid)] == ["execute", "auto_fix"]
    assert "Checks failed" in ex.requests[1].prompt and "AssertionError" in ex.requests[1].prompt
    assert ex.requests[1].resume_conversation_id == "fake-1"


def test_auto_fix_stops_at_its_limit(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    bad = FakeStep(files={"calc.py": "def add(a, b):\n    return 0\n"}, report=DONE)
    out = engine(store, paths, cfg, FakeExecutor([bad, bad, bad, bad])).dispatch(tid)
    assert out.status == "IN_REVIEW" and not out.checks[0]["passed"]
    assert len(store.list_runs(tid)) == 1 + cfg.get("limits", "auto_fix_attempts")
    blockers = review.build_bundle(store, cfg, tid)["approval_blockers"]
    assert any("check test failed" in b for b in blockers)
    with pytest.raises(ValidationError, match="cannot be met"):
        review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")


def test_scope_violation_blocks_approval_and_auto_fix(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(files={"calc.py": "x = (", "README.md": "# changed\n"}, report=DONE)])
    out = engine(store, paths, cfg, ex).dispatch(tid)
    assert out.status == "IN_REVIEW" and len(store.list_runs(tid)) == 1  # no auto-fix with a scope violation
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["scope_violations"] == ["README.md"]
    assert ev["discrepancies"]["changed_not_claimed"] == ["README.md"]


def test_policy_flag_needs_human_approval(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task(scope={"allow": ["calc.py", "pyproject.toml"]}))
    task = store.get_task(tid)
    assert task.status == TaskStatus.AWAITING_APPROVAL and "dependency_change" in task.approval_reasons[0]
    plans.approve_task(store, tid, "user", "a dependency edit is expected")
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD, "pyproject.toml": "[project]\nname='calc2'\n"}, report=DONE)])
    engine(store, paths, cfg, ex).dispatch(tid)
    with pytest.raises(PolicyError, match="NEEDS_HUMAN_APPROVAL"):
        review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")
    task, _ = review.decide(store, cfg, tid, review_doc("NEEDS_HUMAN_APPROVAL"), "claude")
    assert task.status == TaskStatus.NEEDS_HUMAN_APPROVAL
    with pytest.raises(PolicyError):
        review.accept(store, tid, "claude", "fine")
    assert review.accept(store, tid, "user", "dependency change is intended").status == TaskStatus.APPROVED


def test_review_cycle_limit_escalates(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD + f"# v{i}\n"}, report=DONE) for i in range(6)])
    eng = engine(store, paths, cfg, ex)
    fix = [{"severity": "minor", "category": "quality", "message": "m", "instruction": "do better"}]
    for cycle in range(1, cfg.get("limits", "max_review_cycles") + 1):
        eng.dispatch(tid)
        task, _ = review.decide(store, cfg, tid, review_doc("NEEDS_CORRECTION", findings=fix), "claude")
    assert task.status == TaskStatus.FAILED
    assert any(e["kind"] == "task.escalated" for e in store.list_events(subject_type="task", subject_id=tid))
    with pytest.raises(PolicyError):
        review.retry(store, cfg, tid, "claude", "try again")
    task = review.retry(store, cfg, tid, "user", "one more go")
    assert task.status == TaskStatus.READY and store.task_extras(tid)["review_cycles"] == 0
    assert task.review_status == ReviewDecision.NEEDS_CORRECTION


def test_review_validation(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    with pytest.raises(PolicyError, match="not IN_REVIEW"):
        review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")
    engine(store, paths, cfg, FakeExecutor([FakeStep(files={"calc.py": GOOD}, report=DONE)])).dispatch(tid)
    with pytest.raises(PolicyError, match="never by the engine"):
        review.decide(store, cfg, tid, review_doc("APPROVED"), "engine")
    doc = review_doc("APPROVED")
    doc["criteria"] = doc["criteria"][:1] + [{"id": "Z9", "met": True}]
    with pytest.raises(ValidationError) as e:
        review.decide(store, cfg, tid, doc, "claude")
    assert "A2 is not addressed" in str(e.value) and "unknown criterion Z9" in str(e.value)
    with pytest.raises(ValidationError, match="instruction"):
        review.decide(store, cfg, tid, review_doc("NEEDS_CORRECTION", findings=[
            {"severity": "minor", "category": "quality", "message": "vague"}]), "claude")
    with pytest.raises(PolicyError, match="open major finding"):
        review.decide(store, cfg, tid, review_doc("APPROVED", findings=[
            {"severity": "major", "category": "security", "message": "sql injection"}]), "claude")


def test_transient_failures_retry_with_backoff(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    slept = []
    ex = FakeExecutor([FakeStep(status=ExecStatus.FAILED, classification=Classification.TRANSIENT, error="429"),
                       FakeStep(files={"calc.py": GOOD}, report=DONE)])
    eng = workflow.Engine(store, paths, cfg, executor_factory=lambda n: ex, sleep=slept.append, cancel_poll_seconds=0.05)
    assert eng.dispatch(tid).status == "IN_REVIEW"
    assert slept == [cfg.get("limits", "retry_backoff_seconds")]
    assert [r["kind"] for r in store.list_runs(tid)] == ["execute", "retry"]


def test_environment_errors_fail_and_escalate(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(status=ExecStatus.ENV_BLOCKED, classification=Classification.ENV_BLOCKED,
                                error="eligibility check failed")])
    out = engine(store, paths, cfg, ex).dispatch(tid)
    assert out.status == "FAILED" and "eligibility" in out.message
    assert any(e["kind"] == "task.escalated" for e in store.list_events(subject_type="task", subject_id=tid))
    assert review.retry(store, cfg, tid, "claude", "account issue resolved").status == TaskStatus.READY


def test_denied_without_changes_is_retried_once(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    denied = FakeStep(status=ExecStatus.DENIED, classification=Classification.PERMISSION, denied_actions=["RunCommand"])
    ex = FakeExecutor([denied, FakeStep(files={"calc.py": GOOD}, report=DONE)])
    assert engine(store, paths, cfg, ex).dispatch(tid).status == "IN_REVIEW"
    assert "edit files only" in ex.requests[1].prompt or "editing files only" in ex.requests[1].prompt


def test_cancel_stops_a_running_task(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(delay=10, files={"calc.py": GOOD})])

    def cancel_soon():
        time.sleep(0.4)
        s = Store(paths.db_file)
        s.transition_task(tid, TaskStatus.CANCELLED, "user", "changed my mind")
        s.close()

    threading.Thread(target=cancel_soon).start()
    started = time.monotonic()
    out = engine(store, paths, cfg, ex).dispatch(tid)
    assert out.status == "CANCELLED" and time.monotonic() - started < 5
    assert store.list_runs(tid)[0]["status"] == "cancelled"
    assert Path(calc.root, "calc.py").read_text() == CALC


def test_preflight_dirty_scope_and_automatic_branch(store, paths, cfg, calc):
    root = Path(calc.root)
    git(root, "switch", "-q", "main")
    (root / "notes.txt").write_text("my own uncommitted work\n")
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    out = engine(store, paths, cfg, FakeExecutor([FakeStep(files={"calc.py": GOOD}, report=DONE)])).dispatch(tid)
    assert out.status == "IN_REVIEW"
    assert git(root, "branch", "--show-current") == "orch/P1"
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["changed_files"] == ["calc.py"] and ev["new_branch"] == "orch/P1"  # notes.txt is not attributed
    assert (root / "notes.txt").read_text() == "my own uncommitted work\n"

    (t2,) = import_tasks(store, cfg, calc, calc_task("T9"))
    with pytest.raises(PolicyError, match="uncommitted changes inside the task's scope"):
        engine(store, paths, cfg, FakeExecutor([])).dispatch(t2)
    assert store.get_task(t2).status == TaskStatus.READY


def test_snapshot_never_touches_the_users_index(calc):
    root = Path(calc.root)
    (root / "calc.py").write_text(GOOD)
    git(root, "add", "calc.py")
    (root / "new.txt").write_text("untracked\n")
    status = git(root, "status", "--porcelain")
    snap = evidence.snapshot(root)
    assert git(root, "status", "--porcelain") == status
    assert snap.dirty == ["calc.py", "new.txt"]
    assert "new.txt" in git(root, "ls-tree", "--name-only", snap.tree)


def test_recover_interrupts_orphaned_runs(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    store.transition_task(tid, TaskStatus.RUNNING, "engine", "dispatch", updates={"attempt": 1})
    run_id = store.insert_run(tid, 1, "execute", "fake", owner_pid=999_999_999)
    report = workflow.recover(store, "user")
    assert report == [{"run": run_id, "task_id": tid, "result": "interrupted"}]
    assert store.get_task(tid).status == TaskStatus.INTERRUPTED
    assert store.get_run(run_id)["status"] == "interrupted"


def test_scheduler_honours_dependencies_and_concurrency(store, paths, cfg, calc, make_repo):
    other_root = make_repo("other", {"calc.py": CALC})
    git(other_root, "switch", "-q", "-c", "work")
    other, _ = registry.add_project(store, paths, other_root, "user", which=all_tools)
    registry.set_command(store, other.id, "test", "python3 -c 'import calc; assert calc.add(2, 3) == 5'", "user",
                         confirm=True)
    registry.set_field(store, other.id, "executor", "fake", "user")
    a1, a2 = import_tasks(store, cfg, calc, calc_task("T1"), calc_task("T2", depends_on=["T1"]))
    (b1,) = import_tasks(store, cfg, store.get_project(other.id), calc_task("T1"))
    lock = threading.Lock()
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD}, report=DONE, delay=0.2) for _ in range(5)])

    def factory(name):
        with lock:
            return ex

    results = workflow.run_ready(paths, cfg, executor_factory=factory, sleep=lambda s: None)
    by_task = {r["task_id"]: r["status"] for r in results}
    assert by_task == {a1: "IN_REVIEW", b1: "IN_REVIEW"}  # a2 waits for a1's approval
    assert store.get_task(a2).status == TaskStatus.READY


def test_unopted_project_is_skipped_by_the_scheduler(store, paths, cfg, calc):
    registry.set_engine(store, calc.id, False, "user")
    registry.set_field(store, calc.id, "executor", "agy", "user")
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    results = workflow.run_ready(paths, cfg)
    assert results[0]["status"] == "SKIPPED" and "external_engine_not_allowed" in results[0]["message"]
    results = workflow.run_ready(paths, cfg, executor_override="agy")
    assert results[0]["status"] == "SKIPPED"


def test_redaction_and_truncation():
    text = ("token=abcd1234efgh ghp_" + "a" * 30 + " https://u:p4ss@host/x Authorization: Bearer xyz.abc\n"
            "-----BEGIN RSA PRIVATE KEY-----\nAAA\n-----END RSA PRIVATE KEY-----\nAKIAABCDEFGHIJKLMNOP")
    out = redact(text)
    for secret in ("abcd1234efgh", "ghp_aaaa", "p4ss", "xyz.abc", "AAA", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in out
    assert "token=[REDACTED]" in out
    short, cut = truncate_middle("a" * 100 + "END", 40)
    assert cut and short.endswith("END") and "omitted" in short


def test_dependent_task_builds_on_approved_uncommitted_work(store, paths, cfg, calc):
    t1, t2 = import_tasks(store, cfg, calc, calc_task("T1"), calc_task("T2", depends_on=["T1"]))
    tests_v1 = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ex = FakeExecutor({
        t1: [FakeStep(files={"calc.py": GOOD}, report=DONE)],
        t2: [FakeStep(files={"test_calc.py": tests_v1}, report={**DONE, "files_changed": ["test_calc.py"]})],
    })
    eng = engine(store, paths, cfg, ex)
    eng.dispatch(t1)
    review.decide(store, cfg, t1, review_doc("APPROVED"), "claude")

    # T1's approved edit to calc.py is uncommitted and inside T2's scope: it is accounted for.
    assert eng.dispatch(t2).status == "IN_REVIEW"
    ev = review.build_bundle(store, cfg, t2)["evidence"]
    assert ev["changed_files"] == ["test_calc.py"]  # T1's work is not attributed to T2


def test_edits_after_approval_still_block(store, paths, cfg, calc):
    t1, t2 = import_tasks(store, cfg, calc, calc_task("T1"), calc_task("T2", depends_on=["T1"]))
    eng = engine(store, paths, cfg, FakeExecutor({t1: [FakeStep(files={"calc.py": GOOD}, report=DONE)]}))
    eng.dispatch(t1)
    review.decide(store, cfg, t1, review_doc("APPROVED"), "claude")
    Path(calc.root, "calc.py").write_text(GOOD + "# my own tweak\n")
    with pytest.raises(PolicyError, match="calc.py"):
        eng.dispatch(t2)


class CumulativeFake(FakeExecutor):
    """Reports usage as a running total per conversation, like agy."""

    def __init__(self, steps):
        super().__init__(steps)
        self.totals: dict[str, int] = {}

    def capabilities(self):
        import dataclasses
        return dataclasses.replace(super().capabilities(), cumulative_usage=True)

    def run(self, request, cancel=None, on_event=None):
        result = super().run(request, cancel, on_event)
        conv = result.conversation_id
        self.totals[conv] = self.totals.get(conv, 0) + (result.usage or {}).get("input_tokens", 0)
        result.usage = {"input_tokens": self.totals[conv]}
        return result


def test_cumulative_usage_is_recorded_per_run(store, paths, cfg, calc):
    assert workflow.usage_delta({"a": 5}, {"a": 12, "b": 3}) == {"a": 7, "b": 3}
    assert workflow.usage_delta(None, {"a": 1}) == {"a": 1} and workflow.usage_delta({"a": 1}, None) is None
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = CumulativeFake([FakeStep(files={"calc.py": GOOD}, report=DONE, usage={"input_tokens": 100}),
                        FakeStep(files={"test_calc.py": "x = 1\n"}, report=DONE, usage={"input_tokens": 30})])
    eng = engine(store, paths, cfg, ex)
    eng.dispatch(tid)
    review.decide(store, cfg, tid, review_doc("NEEDS_CORRECTION", findings=[
        {"severity": "minor", "category": "tests", "message": "m", "instruction": "add a test"}]), "claude")
    eng.dispatch(tid)
    assert [r["usage"] for r in store.list_runs(tid)] == [{"input_tokens": 100}, {"input_tokens": 30}]
    assert review.build_bundle(store, cfg, tid)["usage_total"] == {"input_tokens": 130}


ARTIFACT_CHECK = ("python3 -c \"import calc, os; assert calc.add(2, 3) == 5; os.makedirs('cache', exist_ok=True); "
                  "open('cache/check.log', 'w').write('ran')\"")


def test_check_artifacts_are_not_attributed_to_the_executor(store, paths, cfg, calc):
    registry.set_command(store, calc.id, "test", ARTIFACT_CHECK, "user", confirm=True)
    t1, t2 = import_tasks(store, cfg, calc, calc_task("T1"),
                          calc_task("T2", depends_on=["T1"], scope={"allow": ["calc.py", "cache/**"]}))
    ex = FakeExecutor({
        t1: [FakeStep(files={"calc.py": GOOD}, report=DONE),
             FakeStep(files={"test_calc.py": "x = 1\n"}, report={**DONE, "files_changed": ["test_calc.py"]})],
        t2: [FakeStep(files={"calc.py": GOOD + "# t2\n"}, report=DONE)],
    })
    eng = engine(store, paths, cfg, ex)
    eng.dispatch(t1)
    ev = review.build_bundle(store, cfg, t1)["evidence"]
    assert ev["changed_files"] == ["calc.py"] and ev["scope_violations"] == []
    assert "cache/check.log" in ev["check_artifacts"]
    assert any(a.startswith("__pycache__/") for a in ev["check_artifacts"])

    review.decide(store, cfg, t1, review_doc("NEEDS_CORRECTION", findings=[
        {"severity": "minor", "category": "tests", "message": "m", "instruction": "add a test"}]), "claude")
    assert eng.dispatch(t1).status == "IN_REVIEW"  # the artifacts do not block the correction
    ev = review.build_bundle(store, cfg, t1)["evidence"]
    assert ev["changed_files"] == ["calc.py", "test_calc.py"] and ev["scope_violations"] == []
    review.decide(store, cfg, t1, review_doc("APPROVED"), "claude")

    assert eng.dispatch(t2).status == "IN_REVIEW"  # nor the dependent task, whose scope covers cache/
    assert review.build_bundle(store, cfg, t2)["evidence"]["changed_files"] == ["calc.py"]


def test_work_moves_off_the_projects_own_default_branch(store, paths, cfg, calc):
    """Found on a real repository whose integration branch is `develop`: agy would have edited it directly."""
    root = Path(calc.root)
    git(root, "branch", "-q", "-m", "work", "develop")
    registry.set_field(store, calc.id, "default_branch", "develop", "user")
    assert not policy.is_protected_branch("develop", cfg)
    assert policy.is_protected_branch("develop", registry.effective_config(cfg, store.get_project(calc.id)))
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    out = engine(store, paths, cfg, FakeExecutor([FakeStep(files={"calc.py": GOOD}, report=DONE)])).dispatch(tid)
    assert out.status == "IN_REVIEW"
    assert git(root, "branch", "--show-current") == "orch/P1"
    assert git(root, "rev-parse", "develop") == git(root, "rev-parse", "orch/P1")  # nothing committed, just moved


def test_sensitive_files_outside_git_are_reported(store, paths, cfg, calc):
    """Found on a real repository: a gitignored .env with live credentials, readable by the executor."""
    root = Path(calc.root)
    (root / ".gitignore").write_text(".env\nsecrets/\nnotes/\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore")
    (root / ".env").write_text("DB_PASSWORD=hunter2\n")
    (root / "secrets").mkdir()
    (root / "secrets" / "api.txt").write_text("token\n")
    (root / "notes").mkdir()
    (root / "notes" / "todo.txt").write_text("not sensitive\n")
    (root / "scratch.txt").write_text("untracked, not sensitive\n")
    assert evidence.readable_secrets(root, cfg) == [".env", "secrets/"]

    class OffMachine(FakeExecutor):
        def capabilities(self):
            return dataclasses.replace(super().capabilities(), sends_code_off_machine=True)

    seen = []
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    eng = engine(store, paths, cfg, OffMachine([FakeStep(files={"calc.py": GOOD}, report=DONE)]),
                 progress=lambda task_id, e: seen.append(e))
    assert eng.dispatch(tid).status == "IN_REVIEW"
    assert any(e.get("event") == "warning" and ".env, secrets/" in e["message"] for e in seen)
    events = [e for e in store.list_events(subject_type="task", subject_id=tid) if e["kind"] == "policy.secrets_readable"]
    assert events and events[0]["data"]["paths"] == [".env", "secrets/"]


def test_fingerprints_see_what_git_ignores(tmp_path):
    root = tmp_path / "r"
    (root / "secrets" / "nested").mkdir(parents=True)
    (root / ".env").write_text("A=1\n")
    (root / "secrets" / "nested" / "key.txt").write_text("k\n")
    (tmp_path / "outside.txt").write_text("not walked\n")
    (root / "secrets" / "link").symlink_to(tmp_path, target_is_directory=True)
    before = evidence.fingerprint(root, [".env", "secrets/", "missing/"])
    assert sorted(before.files) == [".env", "secrets/nested/key.txt"]  # symlinked directories are not followed
    assert all(v.startswith("sha256=") for v in before.files.values()) and before.unchecked == []
    (root / ".env").write_text("A=2\n")
    (root / "secrets" / "nested" / "key.txt").unlink()
    (root / "secrets" / "new.pem").write_text("p\n")
    after = evidence.fingerprint(root, [".env", "secrets/"])
    assert evidence.fingerprint_changes(before, after) == [
        {"path": ".env", "change": "modified"},
        {"path": "secrets/nested/key.txt", "change": "deleted"},
        {"path": "secrets/new.pem", "change": "created"},
    ]


def _ignore_env(root):
    (root / ".gitignore").write_text(".env\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore .env")
    (root / ".env").write_text("DB_PASSWORD=hunter2\n")


def test_executor_edits_to_ignored_sensitive_files_block_approval(store, paths, cfg, calc):
    """Snapshots honour .gitignore, so an executor's edit to .env was invisible to evidence."""
    root = Path(calc.root)
    _ignore_env(root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    seen = []
    eng = engine(store, paths, cfg, FakeExecutor([FakeStep(files={"calc.py": GOOD, ".env": "DB_PASSWORD=x\n"},
                                                           report=DONE)]),
                 progress=lambda task_id, e: seen.append(e))
    out = eng.dispatch(tid)
    assert out.status == "IN_REVIEW" and "scope violation" in out.message
    bundle = review.build_bundle(store, cfg, tid)
    ev = bundle["evidence"]
    assert ev["changed_files"] == ["calc.py"]  # git still can't see .env
    assert ev["ignored_sensitive_changes"] == [{"path": ".env", "change": "modified"}]
    assert ev["scope_violations"] == [".env"]
    assert any(".env (modified, outside git)" in f["detail"] for f in ev["policy_flags"])
    assert any("changes outside the task's scope: .env" in b for b in bundle["approval_blockers"])
    assert "Sensitive files outside git that the executor changed (git can't show them): .env (modified)" \
        in review.render_bundle(bundle)
    assert any(e.get("event") == "warning" and ".env (modified)" in e["message"] for e in seen)
    events = [e for e in store.list_events(subject_type="task", subject_id=tid)
              if e["kind"] == "policy.ignored_sensitive_changed"]
    assert events[0]["data"]["changes"] == [{"path": ".env", "change": "modified"}]
    assert "sha256" not in json.dumps(events) and "hunter2" not in json.dumps(ev)  # never hashes or contents
    with pytest.raises(PolicyError, match="changes outside the task's scope"):
        review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")


def test_ignored_edits_by_a_failed_run_stay_attributed(store, paths, cfg, calc):
    root = Path(calc.root)
    _ignore_env(root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(files={".env": "DB_PASSWORD=x\n"}, status=ExecStatus.FAILED,
                                classification=Classification.TRANSIENT, error="429"),
                       FakeStep(files={"calc.py": GOOD}, report=DONE)])
    assert engine(store, paths, cfg, ex).dispatch(tid).status == "IN_REVIEW"
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["ignored_sensitive_changes"] == [{"path": ".env", "change": "modified"}]
    assert ev["scope_violations"] == [".env"]


def test_the_users_own_edits_to_ignored_files_between_runs_are_not_attributed(store, paths, cfg, calc):
    root = Path(calc.root)
    _ignore_env(root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD}, report=DONE)])
    assert engine(store, paths, cfg, ex).dispatch(tid).status == "IN_REVIEW"
    (root / ".env").write_text("DB_PASSWORD=rotated\n")  # the user, after the run
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["ignored_sensitive_changes"] == [] and ev["scope_violations"] == []
    task, _ = review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")
    assert task.status == TaskStatus.APPROVED


def test_a_directory_over_the_fingerprint_cap_is_left_unchecked(store, paths, cfg, calc, monkeypatch):
    """The cap counted files across all paths, so a new file that sorted first pushed the last one out.

    That file was reported as deleted, which blocked approval, and files past the cap went unchecked silently.
    """
    monkeypatch.setattr(evidence, "FINGERPRINT_MAX_FILES", 3)
    root = Path(calc.root)
    (root / ".gitignore").write_text(".env\nsecrets/\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore")
    (root / ".env").write_text("A=1\n")
    (root / "secrets").mkdir()
    for name in ("b.txt", "c.txt", "d.txt"):
        (root / "secrets" / name).write_text("token\n")
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD, "secrets/a.txt": "new\n"}, report=DONE)])
    assert engine(store, paths, cfg, ex).dispatch(tid).status == "IN_REVIEW"
    bundle = review.build_bundle(store, cfg, tid)
    ev = bundle["evidence"]
    assert ev["ignored_sensitive_changes"] == [] and ev["ignored_sensitive_unchecked"] == ["secrets/"]
    assert bundle["approval_blockers"] == []
    assert "Sensitive directories outside git not checked for changes (too many files): secrets/" \
        in review.render_bundle(bundle)
    events = [e for e in store.list_events(subject_type="task", subject_id=tid)
              if e["kind"] == "policy.ignored_sensitive_unchecked"]
    assert [e["data"]["paths"] for e in events] == [["secrets/"]]

    # Other paths are still compared next to an unchecked directory.
    (root / ".env").write_text("A=2\n")
    after = evidence.fingerprint(root, evidence.ignored_secrets(root, cfg))
    assert sorted(after.files) == [".env"] and after.unchecked == ["secrets/"]
    before = evidence.Fingerprint(files={".env": "old", "secrets/b.txt": "x"})
    assert evidence.fingerprint_changes(before, after) == [{"path": ".env", "change": "modified"}]


def test_ignored_edits_need_a_person_even_when_sensitive_files_are_automatic(store, paths, cfg, calc):
    """A project can make sensitive_file automatic; an edit that no diff can show must still go to a person."""
    root = Path(calc.root)
    _ignore_env(root)
    auto = [*cfg.get("policy", "auto_actions"), "sensitive_file"]
    registry.set_override(store, cfg, calc.id, "policy", "auto_actions", json.dumps(auto), "user")
    (tid,) = import_tasks(store, cfg, calc, calc_task(scope={"allow": ["calc.py", "test_calc.py", ".env"]}))
    assert store.get_task(tid).status == TaskStatus.READY
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD, ".env": "DB_PASSWORD=x\n"}, report=DONE)])
    out = engine(store, paths, cfg, ex).dispatch(tid)
    assert out.status == "IN_REVIEW" and "needs human approval: secrets_change" in out.message
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["scope_violations"] == []
    assert ev["policy_flags"] == [{"action": "secrets_change", "detail": ".env (modified, outside git)"}]
    with pytest.raises(PolicyError, match="NEEDS_HUMAN_APPROVAL"):
        review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")
    task, _ = review.decide(store, cfg, tid, review_doc("NEEDS_HUMAN_APPROVAL"), "claude")
    assert task.status == TaskStatus.NEEDS_HUMAN_APPROVAL


def test_untracked_sensitive_files_are_reported_once(store, paths, cfg, calc):
    """An untracked .env.local that isn't ignored is in the snapshots already, so it isn't fingerprinted too."""
    root = Path(calc.root)
    (root / ".env.local").write_text("A=1\n")
    assert evidence.readable_secrets(root, cfg) == [".env.local"] and evidence.ignored_secrets(root, cfg) == []
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD, ".env.local": "A=2\n"}, report=DONE)])
    assert engine(store, paths, cfg, ex).dispatch(tid).status == "IN_REVIEW"
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["changed_files"] == [".env.local", "calc.py"] and ev["ignored_sensitive_changes"] == []
    assert ev["scope_violations"] == [".env.local"]
    assert ev["policy_flags"] == [{"action": "sensitive_file", "detail": ".env.local"}]
