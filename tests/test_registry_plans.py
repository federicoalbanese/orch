import json

import pytest

from orch import plans, policy, registry
from orch.errors import PolicyError, ValidationError
from orch.models import TaskStatus

from conftest import PY_REPO, all_tools, git, plan_dict, task_dict


def test_add_project_seeds_knowledge_and_defaults(store, paths, make_repo):
    root = make_repo("demo", PY_REPO)
    p, report = registry.add_project(store, paths, root, "user", which=all_tools)
    assert p.id == "demo" and p.allow_external_engine is False
    assert p.commands["test"].run == "make test" and not p.commands["test"].confirmed
    kdir = paths.project_dir("demo")
    assert (kdir / "context.md").read_text().startswith("# demo")
    assert json.loads((kdir / "discovery.json").read_text())["root"] == str(root)
    with pytest.raises(ValidationError, match="already registered"):
        registry.add_project(store, paths, root, "user", which=all_tools)


def test_ids_are_made_unique(store, paths, make_repo, tmp_path):
    a = make_repo("same", {"go.mod": "module a\n"})
    b = tmp_path / "other" / "same"
    b.mkdir(parents=True)
    (b / "go.mod").write_text("module b\n")
    assert registry.add_project(store, paths, a, "user", which=all_tools)[0].id == "same"
    assert registry.add_project(store, paths, b, "user", which=all_tools)[0].id == "same-2"


def test_only_the_user_can_confirm_or_opt_in(store, project):
    with pytest.raises(PolicyError):
        registry.confirm_commands(store, project.id, None, "claude")
    with pytest.raises(PolicyError):
        registry.set_engine(store, project.id, True, "claude")
    registry.set_engine(store, project.id, False, "claude")  # opting out is always allowed
    assert registry.set_engine(store, project.id, True, "user").allow_external_engine


def test_refresh_never_touches_confirmed_commands(store, paths, project):
    registry.set_command(store, project.id, "test", "custom-test", "user", confirm=True)
    (__import__("pathlib").Path(project.root) / "go.mod").write_text("module demo\n")
    p, _, changes = registry.refresh_project(store, paths, project.id, "user", which=all_tools)
    assert p.commands["test"].run == "custom-test" and p.commands["test"].confirmed
    assert "go" in p.languages and any("languages" in c for c in changes)


def test_overrides_validate_and_loosening_needs_user(store, cfg, project):
    registry.set_override(store, cfg, project.id, "limits", "max_review_cycles", "5", "claude")
    assert store.get_project(project.id).overrides == {"limits": {"max_review_cycles": 5}}
    with pytest.raises(ValidationError):
        registry.set_override(store, cfg, project.id, "limits", "max_review_cycles", "0", "user")
    with pytest.raises(PolicyError):
        registry.set_override(store, cfg, project.id, "policy", "auto_actions",
                              '["inspect","edit_in_scope","run_verification","git_branch","git_commit"]', "claude")


def test_import_valid_plan(store, cfg, project):
    tasks = [task_dict("T1"), task_dict("T2", depends_on=["T1"]), task_dict("T3")]
    result = plans.import_plan(store, cfg, plan_dict(project.id, tasks), "claude")
    assert [t.id for t in result.tasks] == ["P1.T1", "P1.T2", "P1.T3"]
    assert all(t.status == TaskStatus.READY for t in result.tasks)
    assert result.tasks[1].depends_on == ["P1.T1"]
    assert plans.execution_waves([t.spec for t in result.tasks]) == [["T1", "T3"], ["T2"]]


@pytest.mark.parametrize(
    "tasks,needle",
    [
        ([task_dict("T1"), task_dict("T1")], "duplicate task key"),
        ([task_dict("T1", depends_on=["T9"])], "unknown task 'T9'"),
        ([task_dict("T1", depends_on=["T2"]), task_dict("T2", depends_on=["T1"])], "cycle"),
        ([task_dict("T1", scope={"allow": ["../outside/**"]})], "'..' not allowed"),
        ([task_dict("T1", scope={"allow": ["/etc/**"]})], "absolute path"),
        ([task_dict("T1", required_checks=["deploy"])], "command 'deploy' is not defined"),
        ([task_dict("T1", acceptance=[{"id": "A", "text": "x", "verify": {"kind": "command"}}])], "no command name"),
        ([task_dict("T1", acceptance=[])], "at least 1"),
    ],
)
def test_invalid_plans_are_rejected_with_reasons(store, cfg, project, tasks, needle):
    with pytest.raises(ValidationError) as e:
        plans.import_plan(store, cfg, plan_dict(project.id, tasks), "claude")
    assert needle in str(e.value)
    assert store.list_plans() == []


def test_policy_gates_put_tasks_in_awaiting_approval(store, cfg, project):
    tasks = [
        task_dict("RISKY", risk="high"),
        task_dict("WIDE", scope={"allow": ["**"]}),
        task_dict("DEPS", scope={"allow": ["pyproject.toml", "src/**"]}),
        task_dict("SECRET", relevant_files=["config/.env.production"], scope={"allow": ["config/**"]}),
        task_dict("MIGR", scope={"allow": ["database/migrations/**"]}, relevant_files=["database/migrations/x.py"]),
        task_dict("PLAIN"),
    ]
    result = plans.import_plan(store, cfg, plan_dict(project.id, tasks), "claude")
    by_key = {t.key: t for t in result.tasks}
    assert by_key["PLAIN"].status == TaskStatus.READY
    for key, code in (("RISKY", "risk_high"), ("WIDE", "unbounded_scope"), ("DEPS", "dependency_change"),
                      ("SECRET", "sensitive_file"), ("MIGR", "migration")):
        t = by_key[key]
        assert t.status == TaskStatus.AWAITING_APPROVAL, key
        assert any(r.startswith(code) for r in t.approval_reasons), (key, t.approval_reasons)


def test_architectural_plans_need_approval(store, cfg, project):
    data = plan_dict(project.id, decision={"summary": "switch queue", "architectural": True})
    t = plans.import_plan(store, cfg, data, "claude").tasks[0]
    assert t.status == TaskStatus.AWAITING_APPROVAL and "architectural_decision" in t.approval_reasons[0]


def test_approval_flow(store, cfg, project):
    result = plans.import_plan(store, cfg, plan_dict(project.id, [task_dict("T1", risk="high")]), "claude")
    tid = result.tasks[0].id
    with pytest.raises(PolicyError):
        plans.approve_task(store, tid, "claude", "looks fine")
    assert plans.approve_task(store, tid, "user", "ok").status == TaskStatus.READY
    with pytest.raises(PolicyError, match="not AWAITING_APPROVAL"):
        plans.approve_task(store, tid, "user", "again")
    assert plans.cancel_task(store, tid, "claude", "dropped").status == TaskStatus.CANCELLED


def test_dispatch_blockers(store, cfg, project):
    spec = plans.import_plan(store, cfg, plan_dict(project.id), "claude").tasks[0].spec
    codes = [b.code for b in policy.dispatch_blockers(spec, project, "agy", True)]
    assert codes == ["external_engine_not_allowed"]
    assert policy.dispatch_blockers(spec, project, "fake", False) == []
    registry.set_command(store, project.id, "test", "make test", "claude")  # resets to unconfirmed
    p = store.get_project(project.id)
    assert [b.code for b in policy.dispatch_blockers(spec, p, "fake", False)] == ["command_unconfirmed"]


def test_import_warns_about_unopted_project(store, cfg, project):
    result = plans.import_plan(store, cfg, plan_dict(project.id), "claude")
    assert any("external_engine_not_allowed" in w for w in result.warnings)


def test_protected_branches(cfg):
    assert policy.is_protected_branch("main", cfg) and policy.is_protected_branch("release/1.2", cfg)
    assert not policy.is_protected_branch("feature/main-menu", cfg)
