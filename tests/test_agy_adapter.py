import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from orch.executors.agy import AgyExecutor, classify, extract_report
from orch.executors.base import Classification, ExecRequest, ExecStatus, Mode
from orch.schema import REPORT_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures" / "agy"


def result_line(**result) -> str:
    base = {"conversation_id": "c-1", "status": "SUCCESS", "response": "", "duration_seconds": 1, "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": 2}}
    base.update(result)
    return json.dumps({"event": "result", "result": base})


@pytest.fixture
def agy(tmp_path, monkeypatch):
    script = tmp_path / "agy"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).parent / 'fake_agy.py'} \"$@\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_AGY_ARGV", str(argv_file))

    def make(fixture_text=None, fixture=None, **env):
        if fixture_text is not None:
            fixture = tmp_path / "fixture.jsonl"
            fixture.write_text(fixture_text)
        if fixture is not None:
            monkeypatch.setenv("FAKE_AGY_FIXTURE", str(fixture))
        for k, v in env.items():
            monkeypatch.setenv(f"FAKE_AGY_{k.upper()}", v)
        return AgyExecutor(binary=str(script), brain_dir=str(tmp_path / "brain"))

    make.workdir = workdir
    make.argv = lambda: json.loads(argv_file.read_text())
    return make


def req(workdir, **kw):
    base = dict(task_id="P1.T1", attempt=1, workdir=str(workdir), prompt="do it", report_schema=REPORT_SCHEMA,
                timeout_seconds=60)
    base.update(kw)
    return ExecRequest(**base)


def test_recorded_edit_run(agy):
    ex = agy(fixture=FIXTURES / "edit_success.jsonl", edits=json.dumps({"calc.py": "def neg(x):\n    return -x\n"}))
    events = []
    r = ex.run(req(agy.workdir), on_event=events.append)
    assert r.status == ExecStatus.SUCCEEDED, r.error
    assert r.conversation_id == "eb5f7173-d03a-4e54-83e4-b77904a891e1"
    assert r.report["files_changed"] == ["calc.py"] and r.report["status"] == "DONE"
    assert r.usage["input_tokens"] == 55858
    kinds = [e["event"] for e in events]
    assert kinds[0] == "process" and "init" in kinds and "tool" in kinds and kinds[-1] == "result"
    argv = agy.argv()
    assert argv[:2] == ["-p", "do it"] and "--mode" in argv and argv[argv.index("--mode") + 1] == "accept-edits"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert "$id" not in schema and schema["required"] == REPORT_SCHEMA["required"]
    assert "--conversation" not in argv


def test_resume_model_and_read_only_flags(agy):
    ex = agy(fixture_text=result_line(structured_output={"status": "DONE", "files_changed": [], "summary": "s",
                                                         "unresolved": []}))
    ex.run(req(agy.workdir, resume_conversation_id="c-9", model="gemini-x", mode=Mode.READ_ONLY))
    argv = agy.argv()
    assert argv[argv.index("--conversation") + 1] == "c-9" and argv[argv.index("--model") + 1] == "gemini-x"
    assert "--mode" not in argv


def test_print_timeout_is_not_success(agy):
    ex = agy(fixture=FIXTURES / "print_timeout.jsonl",
             stderr=(FIXTURES / "print_timeout.stderr").read_text())
    r = ex.run(req(agy.workdir))
    assert r.status == ExecStatus.TIMED_OUT and r.classification == Classification.TIMEOUT


@pytest.mark.parametrize(
    "stderr,result,status,classification",
    [
        ("", {"denied_actions": [{"action": "command", "display_name": "RunCommand"}]}, ExecStatus.DENIED,
         Classification.PERMISSION),
        ("Error: HTTP 400 User location is not supported for the API use.", None, ExecStatus.ENV_BLOCKED,
         Classification.ENV_BLOCKED),
        ("HTTP 403 SUBSCRIPTION_REQUIRED (#3501)", None, ExecStatus.ENV_BLOCKED, Classification.ENV_BLOCKED),
        ("Eligibility check failed: region", None, ExecStatus.ENV_BLOCKED, Classification.ENV_BLOCKED),
        ("Error: HTTP 429 Too Many Requests RESOURCE_EXHAUSTED", None, ExecStatus.FAILED, Classification.TRANSIENT),
        ("boom", None, ExecStatus.FAILED, Classification.EXECUTOR_ERROR),
        ("", {"status": "ERROR"}, ExecStatus.FAILED, Classification.EXECUTOR_ERROR),
    ],
)
def test_classification(stderr, result, status, classification):
    res = None if result is None else {"status": "SUCCESS", "response": "", **result}
    got = classify(1 if result is None else 0, stderr, res, cancelled=False, hard_timeout=False)
    assert got[:2] == (status, classification)


def test_model_words_never_trigger_environment_errors():
    res = {"status": "SUCCESS", "response": "Implemented the eligibility check failed path and HTTP 429 handling",
           "structured_output": {"summary": "user location is not supported now handled"}}
    assert classify(0, "", res, False, False)[0] == ExecStatus.SUCCEEDED


def test_denied_run_end_to_end(agy):
    ex = agy(fixture_text=result_line(denied_actions=[{"action": "command", "display_name": "RunCommand"}]),
             stderr="jetski: no output produced — a tool required the \"command\" permission")
    r = ex.run(req(agy.workdir))
    assert r.status == ExecStatus.DENIED and r.denied_actions == ["RunCommand"]


def test_report_from_fenced_response_and_invalid_reports(tmp_path):
    rep = {"status": "DONE", "files_changed": [f"{tmp_path}/a.py", "file://" + str(tmp_path / "b/c.py")],
           "summary": "x", "unresolved": []}
    report, problem = extract_report({"response": "Done.\n```json\n" + json.dumps(rep) + "\n```"}, tmp_path)
    assert problem is None and report["files_changed"] == ["a.py", "b/c.py"]
    report, problem = extract_report({"structured_output": {"status": "MAYBE"}}, tmp_path)
    assert report is None and "schema" in problem
    assert extract_report({"response": "no json here"}, tmp_path) == (None, "the executor returned no structured report")


def test_cancel_kills_the_process_group(agy):
    ex = agy(fixture_text="\n".join([json.dumps({"event": "init", "conversation_id": "c-1"})] * 50), delay="0.2")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    pids = []
    r = ex.run(req(agy.workdir), cancel=cancel, on_event=lambda e: pids.append(e.get("pid")))
    assert r.status == ExecStatus.CANCELLED and time.monotonic() - started < 8
    pid = next(p for p in pids if p)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_oversized_prompt_and_missing_binary(agy, tmp_path):
    r = agy(fixture_text=result_line()).run(req(agy.workdir, prompt="x" * 130_000))
    assert r.status == ExecStatus.FAILED and "at most" in r.error
    missing = AgyExecutor(binary=str(tmp_path / "nope"))
    assert not missing.health().ok
    assert missing.run(req(agy.workdir)).status == ExecStatus.ENV_BLOCKED


def test_health_reports_version(agy):
    h = agy().health()
    assert h.ok and h.version == "9.9.9-fake"


@pytest.mark.skipif(os.environ.get("ORCH_LIVE_AGY") != "1", reason="set ORCH_LIVE_AGY=1 to call the real agy")
def test_live_agy_read_only(tmp_path):
    (tmp_path / "x.py").write_text("def f():\n    return 1\n")
    r = AgyExecutor().run(ExecRequest("LIVE", 1, str(tmp_path), "Without editing anything, report status DONE, "
                                      "files_changed empty, summary 'live ok', unresolved empty.",
                                      mode=Mode.READ_ONLY, report_schema=REPORT_SCHEMA, timeout_seconds=120))
    assert r.status == ExecStatus.SUCCEEDED, (r.error, r.response_text)
    assert r.report and r.report["status"] == "DONE"
