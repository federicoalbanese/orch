import json

import pytest

from orch.cli import main, resolve_actor
from orch.errors import ValidationError

from conftest import PY_REPO, plan_dict


def run(capsys, home, *argv, env_actor=None, monkeypatch=None):
    code = main(["--home", str(home), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_end_to_end_cli(tmp_path, capsys, make_repo, monkeypatch):
    monkeypatch.delenv("ORCH_ACTOR", raising=False)
    home = tmp_path / "home"
    repo = make_repo("demo", PY_REPO)
    assert run(capsys, home, "init")[0] == 0
    assert (home / "config" / "config.toml").exists()

    code, out, _ = run(capsys, home, "project", "discover", str(repo))
    assert code == 0 and "make test" in out and "nothing runs until you confirm" in out

    code, out, _ = run(capsys, home, "--json", "project", "add", str(repo))
    assert code == 0 and json.loads(out)["project"]["id"] == "demo"

    monkeypatch.setenv("ORCH_ACTOR", "claude")
    code, _, err = run(capsys, home, "project", "command", "confirm", "demo")
    assert code == 5 and "must be done by the user" in err
    code, _, err = run(capsys, home, "--actor", "user", "project", "command", "confirm", "demo")
    assert code == 3 and "environment wins" in err

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_dict("demo")))
    code, out, _ = run(capsys, home, "--json", "plan", "import", str(plan))
    assert code == 0
    data = json.loads(out)
    assert data["plan_id"] == "P1" and any("command_unconfirmed" in w for w in data["warnings"])

    monkeypatch.delenv("ORCH_ACTOR")
    assert run(capsys, home, "project", "command", "confirm", "demo")[0] == 0
    code, out, _ = run(capsys, home, "plan", "show", "P1")
    assert code == 0 and "Execution waves: [T1]" in out
    code, out, _ = run(capsys, home, "--json", "task", "ready")
    assert [t["id"] for t in json.loads(out)] == ["P1.T1"]
    code, out, _ = run(capsys, home, "status")
    assert "Ready to dispatch: P1.T1" in out
    code, out, _ = run(capsys, home, "--json", "log", "--task", "P1.T1")
    assert json.loads(out)[0]["kind"] == "task.created"


def test_cli_errors_are_structured(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ORCH_ACTOR", raising=False)
    home = tmp_path / "home"
    code, _, err = run(capsys, home, "--json", "project", "show", "missing")
    assert code == 4 and json.loads(err)["error"] == "NotFoundError"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code, _, err = run(capsys, home, "--json", "plan", "import", str(bad))
    assert code == 3 and json.loads(err)["details"]


def test_schema_command(tmp_path, capsys):
    assert main(["--home", str(tmp_path), "schema", "plan"]) == 0
    assert json.loads(capsys.readouterr().out)["$id"] == "orch/plan/v1"


def test_resolve_actor():
    assert resolve_actor(None, {}) == "user"
    assert resolve_actor("claude", {}) == "claude"
    assert resolve_actor(None, {"ORCH_ACTOR": "claude"}) == "claude"
    with pytest.raises(ValidationError):
        resolve_actor("user", {"ORCH_ACTOR": "claude"})
    with pytest.raises(ValidationError):
        resolve_actor(None, {"ORCH_ACTOR": "root"})


def test_cli_run_review_decide(tmp_path, capsys, make_repo, monkeypatch):
    monkeypatch.delenv("ORCH_ACTOR", raising=False)
    home = tmp_path / "home"
    repo = make_repo("demo", PY_REPO)
    run(capsys, home, "project", "add", str(repo))
    assert run(capsys, home, "project", "command", "set", "demo", "test", "true", "--confirm")[0] == 0
    run(capsys, home, "project", "command", "remove", "demo", "lint")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_dict("demo")))
    run(capsys, home, "plan", "import", str(plan))

    code, out, _ = run(capsys, home, "--json", "run", "--executor", "fake")
    assert code == 0, out
    assert json.loads(out)[0]["status"] == "IN_REVIEW"
    code, out, _ = run(capsys, home, "review", "show", "P1.T1")
    assert "Engine verdict" in out and "nothing blocks approval" in out
    code, out, _ = run(capsys, home, "status")
    assert "Waiting for review" in out and "P1.T1" in out

    decision = tmp_path / "review.json"
    decision.write_text(json.dumps({"decision": "APPROVED", "summary": "ok", "findings": [],
                                    "criteria": [{"id": "A1", "met": True}, {"id": "A2", "met": True}]}))
    code, out, err = run(capsys, home, "--actor", "claude", "review", "decide", "P1.T1", "--file", str(decision))
    assert code == 0, err
    assert "APPROVED" in out
    code, out, _ = run(capsys, home, "--json", "log", "--task", "P1.T1", "--limit", "50")
    kinds = [e["kind"] for e in json.loads(out)]
    assert "review.recorded" in kinds and "run.finished" in kinds
    assert run(capsys, home, "recover")[1].strip() == "nothing to recover"


def test_skill_install(tmp_path, capsys):
    dest = tmp_path / "skills" / "orchestrate"
    assert main(["--home", str(tmp_path), "skill", "install", "--dest", str(dest)]) == 0
    installed = dest / "SKILL.md"
    assert installed.read_text().startswith("---\nname: orchestrate")
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "skill", "install", "--dest", str(dest)]) == 0
    assert "unchanged" in capsys.readouterr().out
    installed.write_text("local edits")
    assert main(["--home", str(tmp_path), "skill", "install", "--dest", str(dest)]) == 1
    assert "--force" in capsys.readouterr().err
    assert main(["--home", str(tmp_path), "skill", "install", "--dest", str(dest), "--force"]) == 0
    assert installed.read_text().startswith("---\nname: orchestrate")
