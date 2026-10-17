import json
import subprocess
from pathlib import Path

import pytest

from orch import commits, registry, review
from orch.cli import main
from orch.errors import OrchError, PolicyError
from orch.executors.fake import FakeExecutor, FakeStep
from orch.models import TaskStatus

from conftest import DONE, GOOD, calc_task, engine, git, import_tasks, review_doc

TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
ARTIFACT_CHECK = ("python3 -c \"import calc, os; assert calc.add(2, 3) == 5; os.makedirs('cache', exist_ok=True); "
                  "open('cache/check.log', 'w').write('ran')\"")


def approve(store, paths, cfg, tid, files, report=DONE):
    ex = FakeExecutor([FakeStep(files=files, report=report)])
    out = engine(store, paths, cfg, ex).dispatch(tid)
    assert out.status == "IN_REVIEW", out.message
    task, _ = review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")
    assert task.status == TaskStatus.APPROVED


def head(root):
    return git(root, "rev-parse", "HEAD")


def status(root):
    """`git status --porcelain` lines, without the bytecode the engine's own check runs leave behind."""
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root, capture_output=True,
                         text=True, check=True).stdout
    return sorted(line for line in out.splitlines() if "__pycache__" not in line)


def committed_files(root, rev="HEAD"):
    return sorted(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", rev).splitlines())


def test_commits_exactly_the_tasks_files_and_never_pushes(store, paths, cfg, calc, tmp_path):
    root = Path(calc.root)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "origin", "work")
    pushed = git(root, "ls-remote", "origin")
    registry.set_command(store, calc.id, "test", ARTIFACT_CHECK, "user", confirm=True)
    (tid,) = import_tasks(store, cfg, calc, calc_task(title="Fix add() so it adds"))
    approve(store, paths, cfg, tid, {"calc.py": GOOD, "test_calc.py": TEST_CALC},
            {**DONE, "files_changed": ["calc.py", "test_calc.py"]})
    (root / "README.md").write_text("# calc\n\nmy own staged edit\n")
    git(root, "add", "README.md")
    (root / "notes.txt").write_text("my own untracked notes\n")
    before = head(root)

    plan = commits.prepare(store, cfg, [tid])
    assert plan.files == ["calc.py", "test_calc.py"] and plan.branch == "work" and head(root) == before
    assert plan.message.splitlines()[0] == "Fix add() so it adds"
    assert "- A1: add() adds (`test` passed)" in plan.message and "Checks run by orch: test passed." in plan.message
    assert plan.message.rstrip().endswith("Orch-Task: P1.T1")
    assert "co-authored-by" not in plan.message.lower()

    with pytest.raises(PolicyError, match="must be done by the user"):
        commits.create(store, cfg, [tid], "claude")
    assert head(root) == before

    plan, sha = commits.create(store, cfg, [tid], "user")
    assert head(root) == sha != before
    assert committed_files(root) == ["calc.py", "test_calc.py"] == plan.committed_files
    assert git(root, "log", "-1", "--format=%B").strip() == plan.message.strip()
    assert status(root) == ["?? cache/check.log", "?? notes.txt", "M  README.md"]  # the user's staged edit
    # is still staged and not committed; untracked work and check artifacts are left alone
    assert git(root, "ls-remote", "origin") == pushed  # nothing was pushed
    events = store.list_events(subject_type="task", subject_id=tid)
    assert events[0]["kind"] == "git.committed" and events[0]["data"]["commit"] == sha
    assert events[0]["actor"] == "user"

    with pytest.raises(PolicyError, match="nothing to commit"):
        commits.prepare(store, cfg, [tid])


def test_only_approved_tasks_with_unchanged_content(store, paths, cfg, calc):
    root = Path(calc.root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    with pytest.raises(PolicyError, match="only APPROVED tasks"):
        commits.prepare(store, cfg, [tid])
    approve(store, paths, cfg, tid, {"calc.py": GOOD})
    (root / "calc.py").write_text(GOOD + "# edited after the review\n")
    with pytest.raises(PolicyError, match="changed since the review approved them.*calc.py"):
        commits.create(store, cfg, [tid], "user")
    assert status(root) == [" M calc.py"]


def test_a_file_changed_again_by_a_later_task_needs_both(store, paths, cfg, calc):
    root = Path(calc.root)
    t1, t2 = import_tasks(store, cfg, calc, calc_task("T1"), calc_task("T2", depends_on=["T1"]))
    approve(store, paths, cfg, t1, {"calc.py": GOOD})
    approve(store, paths, cfg, t2, {"calc.py": GOOD + "\n\ndef sub(a, b):\n    return a - b\n",
                                    "test_calc.py": TEST_CALC})
    with pytest.raises(PolicyError, match=f"calc.py was changed again by {t2} after {t1}.*orch task commit {t1} {t2}"):
        commits.prepare(store, cfg, [t1])
    plan, _ = commits.create(store, cfg, [t1, t2], "user")
    assert plan.message.splitlines()[0] == "Demo plan"
    assert "Orch-Task: P1.T1\nOrch-Task: P1.T2" in plan.message
    assert committed_files(root) == ["calc.py", "test_calc.py"]
    assert status(root) == []


def test_policy_can_make_commits_automatic(store, paths, cfg, calc):
    auto = cfg.get("policy", "auto_actions") + ["git_commit"]
    registry.set_override(store, cfg, calc.id, "policy", "auto_actions", json.dumps(auto), "user")
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    approve(store, paths, cfg, tid, {"calc.py": GOOD})
    _, sha = commits.create(store, cfg, [tid], "claude")
    assert head(Path(calc.root)) == sha


def test_refused_on_a_protected_branch(store, paths, cfg, calc):
    root = Path(calc.root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    approve(store, paths, cfg, tid, {"calc.py": GOOD})
    git(root, "switch", "-q", "main")
    with pytest.raises(PolicyError, match="protected branch 'main'"):
        commits.create(store, cfg, [tid], "user")


def test_refused_on_the_projects_own_default_branch(store, paths, cfg, calc):
    """Found on a real repository whose integration branch is `develop`, which no default pattern names."""
    root = Path(calc.root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    approve(store, paths, cfg, tid, {"calc.py": GOOD})
    git(root, "branch", "-q", "-m", "work", "develop")
    registry.set_field(store, calc.id, "default_branch", "develop", "user")
    with pytest.raises(PolicyError, match="protected branch 'develop'"):
        commits.create(store, cfg, [tid], "user")


def test_given_messages_lose_attribution_trailers(store, paths, cfg, calc):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    approve(store, paths, cfg, tid, {"calc.py": GOOD})
    message = "Fix add\n\nBody.\n\nCo-Authored-By: Some Agent <agent@example.com>\nco-authored-by: x <y@z>\n"
    plan, _ = commits.create(store, cfg, [tid], "user", message)
    assert plan.message == "Fix add\n\nBody.\n"
    assert "co-authored-by" not in git(Path(calc.root), "log", "-1", "--format=%B").lower()


def test_deletions_and_literal_paths(store, paths, cfg, calc):
    """A deleted file is committed as a deletion; file names are paths, never patterns."""
    root = Path(calc.root)
    (root / "old_calc.py").write_text("legacy = True\n")
    git(root, "add", "old_calc.py")
    git(root, "commit", "-q", "-m", "legacy module")
    (tid,) = import_tasks(store, cfg, calc, calc_task(scope={"allow": ["calc*.py", "old_calc.py"]}))
    ex = FakeExecutor([FakeStep(files={"calc.py": GOOD, "old_calc.py": None, "calc_[v2].py": "v = 2\n"},
                                report={**DONE, "files_changed": ["calc.py", "old_calc.py", "calc_[v2].py"]})])
    engine(store, paths, cfg, ex).dispatch(tid)
    review.decide(store, cfg, tid, review_doc("NEEDS_HUMAN_APPROVAL"), "claude")  # deletions need a human
    review.accept(store, tid, "user", "the legacy module goes")
    (root / "calc_v.py").write_text("not part of the task\n")  # the glob calc_[v2].py would match this
    plan, _ = commits.create(store, cfg, [tid], "user")
    assert plan.files == ["calc.py", "calc_[v2].py", "old_calc.py"]
    assert committed_files(root) == ["calc.py", "calc_[v2].py", "old_calc.py"]
    assert "old_calc.py" not in git(root, "ls-tree", "--name-only", "HEAD").splitlines()
    assert status(root) == ["?? calc_v.py"]


def test_a_failing_hook_leaves_nothing_behind(store, paths, cfg, calc):
    root = Path(calc.root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    approve(store, paths, cfg, tid, {"calc.py": GOOD, "test_calc.py": TEST_CALC},
            {**DONE, "files_changed": ["calc.py", "test_calc.py"]})
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    before = head(root)
    with pytest.raises(OrchError, match="lint says no"):
        commits.create(store, cfg, [tid], "user")
    assert head(root) == before
    assert status(root) == [" M calc.py", "?? test_calc.py"]  # the new file is untracked again
    assert not [e for e in store.list_events(subject_type="task", subject_id=tid) if e["kind"] == "git.committed"]


def test_cli_commit(tmp_path, capsys, store, paths, cfg, calc, monkeypatch):
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    approve(store, paths, cfg, tid, {"calc.py": GOOD})
    home = str(paths.data_dir.parent)
    monkeypatch.setenv("ORCH_ACTOR", "claude")
    assert main(["--home", home, "task", "commit", tid, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would commit 1 file(s) on work" in out and "calc.py" in out and "Orch-Task: P1.T1" in out
    message = tmp_path / "msg.txt"
    message.write_text("Make add() add\n")
    assert main(["--home", home, "task", "commit", tid, "--dry-run", "--message-file", str(message)]) == 0
    assert f"To commit: orch task commit {tid} --message-file {message}" in capsys.readouterr().out
    assert main(["--home", home, "task", "commit", tid]) == 5
    assert "must be done by the user" in capsys.readouterr().err
    monkeypatch.delenv("ORCH_ACTOR")
    assert main(["--home", home, "--json", "task", "commit", tid, "--message-file", str(message)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["committed_files"] == ["calc.py"] and data["message"] == "Make add() add\n"
    assert data["commit"] == head(Path(calc.root))
    assert subprocess.run(["git", "log", "-1", "--format=%s"], cwd=calc.root, capture_output=True,
                          text=True).stdout.strip() == "Make add() add"


def test_a_note_when_the_repository_uses_conventional_commits(store, paths, cfg, calc):
    """Found on a real repository whose subjects are `fix: ...`: orch's generated subject had no type."""
    root = Path(calc.root)
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    approve(store, paths, cfg, tid, {"calc.py": GOOD})
    assert commits.prepare(store, cfg, [tid]).notes == []  # one "initial" commit: no convention to follow
    for n in range(5):
        (root / "README.md").write_text(f"# calc {n}\n")
        git(root, "commit", "-q", "-m", f"docs: revise the readme ({n})", "--", "README.md")
    plan = commits.prepare(store, cfg, [tid])
    assert len(plan.notes) == 1 and "5 of the last 6 commit subjects" in plan.notes[0]
    assert commits.prepare(store, cfg, [tid], "fix: make add() add\n").notes == []
    assert commits.prepare(store, cfg, [tid], "Make add() add\n").notes == plan.notes
