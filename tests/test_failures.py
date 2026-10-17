"""Failure injection: a killed engine, cancelled subprocesses, exhausted retries, corrupted executor output.

These tests drive the real agy adapter against tests/fake_agy.py, a real subprocess. One of them
runs `orch run` in a process of its own and kills it with SIGKILL halfway through a run.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orch import evidence, registry, review, workflow
from orch.cli import main
from orch.executors.agy import AgyExecutor
from orch.executors.base import Classification, ExecRequest, ExecStatus
from orch.executors.fake import FakeExecutor, FakeStep
from orch.models import TaskStatus
from orch.schema import REPORT_SCHEMA
from orch.store import Store

from conftest import CALC, DONE, GOOD, agy_result, calc_task, engine, import_tasks, review_doc

SRC = Path(__file__).resolve().parents[1] / "src"
INIT = json.dumps({"event": "init", "conversation_id": "c-1"})


def wait_for(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def gone(pid: int) -> bool:
    """True once the process has exited (a zombie that is not reaped yet counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return not state or state.startswith("Z")


@pytest.fixture
def agy_env(tmp_path, monkeypatch, fake_agy_bin):
    """Point the real agy adapter at the stand-in binary. Returns a function that scripts its next runs."""
    monkeypatch.setenv("ORCH_AGY_BIN", str(fake_agy_bin))
    monkeypatch.setenv("ORCH_AGY_BRAIN_DIR", str(tmp_path / "brain"))
    counter = iter(range(1000))

    def script(lines=(), **env):
        for key in [k for k in os.environ if k.startswith("FAKE_AGY_")]:
            monkeypatch.delenv(key)
        fixture = tmp_path / f"stream-{next(counter)}.jsonl"
        fixture.write_bytes(b"\n".join(x if isinstance(x, bytes) else x.encode() for x in lines))
        monkeypatch.setenv("FAKE_AGY_FIXTURE", str(fixture))
        for key, value in env.items():
            monkeypatch.setenv(f"FAKE_AGY_{key.upper()}", value if isinstance(value, str) else json.dumps(value))

    return script


@pytest.fixture
def agy_calc(store, calc):
    registry.set_field(store, calc.id, "executor", "agy", "user")
    return store.get_project(calc.id)


def agy_engine(store, paths, cfg, **kw):
    return workflow.Engine(store, paths, cfg, sleep=kw.pop("sleep", lambda s: None), cancel_poll_seconds=0.05, **kw)


# -- a killed engine ------------------------------------------------------------


def test_engine_killed_mid_run_then_recover_and_retry(store, paths, cfg, agy_calc, agy_env, tmp_path, capsys):
    (tid,) = import_tasks(store, cfg, agy_calc, calc_task())
    root = Path(agy_calc.root)
    pidfile = tmp_path / "agy.pid"
    unfinished = "def add(a, b):\n    return a +\n"
    agy_env([INIT, agy_result(structured_output=DONE)], delay="30", partial={"calc.py": unfinished},
            pidfile=str(pidfile))
    env = {k: v for k, v in os.environ.items() if k != "ORCH_ACTOR"}
    env["PYTHONPATH"] = str(SRC)
    home = str(paths.data_dir.parent)
    orch_run = subprocess.Popen([sys.executable, "-m", "orch", "--home", home, "run", "--quiet"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        run = wait_for(lambda: next((r for r in store.list_runs(tid) if r["pid"]), None), "the executor to start")
        wait_for(lambda: (root / "calc.py").read_text() == unfinished, "the executor's first edit")
        os.kill(orch_run.pid, signal.SIGKILL)
        orch_run.wait(timeout=10)
    finally:
        if orch_run.poll() is None:
            orch_run.kill()
    agy_pid = int(pidfile.read_text())
    assert run["pid"] == agy_pid and not gone(agy_pid)  # the executor outlives the engine (its own session)
    assert store.get_task(tid).status == TaskStatus.RUNNING

    def cli(*argv):
        code = main(["--home", home, "--json", *argv])
        return code, json.loads(capsys.readouterr().out or "null")

    code, report = cli("recover")
    assert code == 0 and report[0]["result"] == "executor still alive"
    assert store.get_task(tid).status == TaskStatus.RUNNING
    code, report = cli("recover", "--kill")
    assert code == 0 and report[0]["result"] == "interrupted"
    wait_for(lambda: gone(agy_pid), "the orphaned executor to stop")
    assert store.get_task(tid).status == TaskStatus.INTERRUPTED
    assert (root / "calc.py").read_text() == unfinished  # nothing is reverted automatically

    code, task = cli("task", "retry", tid, "--reason", "engine was killed")
    assert code == 0 and task["status"] == "READY"
    agy_env([INIT, agy_result(structured_output=DONE)], edits={"calc.py": GOOD})
    out = agy_engine(store, paths, cfg).dispatch(tid)
    assert out.status == "IN_REVIEW", out.message
    runs = store.list_runs(tid)
    assert [(r["attempt"], r["kind"], r["status"]) for r in runs] == [(1, "execute", "interrupted"),
                                                                     (2, "execute", "succeeded")]
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["changed_files"] == ["calc.py"] and ev["checks"][0]["passed"]
    assert "-    return a - b" in ev["patch"]  # the diff starts from before the killed run


def test_interrupted_run_that_left_files_is_attributed_on_retry(store, paths, cfg, agy_calc, agy_env, tmp_path):
    """The killed run's unfinished edits stay attributed to the executor, even when the retry leaves them alone."""
    (tid,) = import_tasks(store, cfg, agy_calc, calc_task())
    root = Path(agy_calc.root)
    before = evidence.snapshot(root)
    store.transition_task(tid, TaskStatus.RUNNING, "engine", "dispatch",
                          updates={"attempt": 1, "baseline_tree": before.tree})
    run_id = store.insert_run(tid, 1, "execute", "agy", owner_pid=999_999_999)
    store.update_run(run_id, before_tree=before.tree)
    (root / "test_calc.py").write_text("from calc import add\n")  # the dead run's work in progress
    assert workflow.recover(store, "user")[0]["result"] == "interrupted"
    assert store.get_run(run_id)["after_tree"]
    review.retry(store, cfg, tid, "claude", "engine died")

    agy_env([INIT, agy_result(structured_output=DONE)], edits={"calc.py": GOOD})
    assert agy_engine(store, paths, cfg).dispatch(tid).status == "IN_REVIEW"
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["changed_files"] == ["calc.py", "test_calc.py"]


# -- cancelling a real executor process ------------------------------------------------


def test_cancel_stops_a_real_executor_process(store, paths, cfg, agy_calc, agy_env, tmp_path, capsys):
    (tid,) = import_tasks(store, cfg, agy_calc, calc_task())
    pidfile = tmp_path / "agy.pid"
    agy_env([INIT] * 100 + [agy_result(structured_output=DONE)], delay="0.2", pidfile=str(pidfile),
            partial={"calc.py": "def add(a, b):\n    return\n"}, edits={"calc.py": GOOD})
    outcome = {}

    def dispatch():
        with Store(paths.db_file) as own:  # each engine worker has its own connection, as in run_ready
            outcome["out"] = agy_engine(own, paths, cfg).dispatch(tid)

    worker = threading.Thread(target=dispatch)
    worker.start()
    wait_for(lambda: pidfile.exists() and store.get_task(tid).status == TaskStatus.RUNNING, "the executor to start")
    agy_pid = int(pidfile.read_text())
    home = str(paths.data_dir.parent)
    assert main(["--home", home, "task", "cancel", tid, "--reason", "wrong approach"]) == 0  # as from another terminal
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert outcome["out"].status == "CANCELLED"
    wait_for(lambda: gone(agy_pid), "the executor process group to stop", timeout=15)
    run = store.list_runs(tid)[0]
    assert run["status"] == "cancelled" and run["classification"] == "cancelled"
    assert store.task_evidence(tid) == []  # nothing was verified or attributed after the cancel
    assert Path(agy_calc.root, "calc.py").read_text() == "def add(a, b):\n    return\n"  # left in place, not reverted
    capsys.readouterr()


# -- transient failures until the retries run out ---------------------------------------


def test_transient_retries_back_off_then_fail(store, paths, cfg, agy_calc, agy_env):
    (tid,) = import_tasks(store, cfg, agy_calc, calc_task())
    agy_env([], stderr="Error: HTTP 429 Too Many Requests: RESOURCE_EXHAUSTED", exit="1")
    slept = []
    out = agy_engine(store, paths, cfg, sleep=slept.append).dispatch(tid)
    retries, backoff = cfg.get("limits", "transient_retries"), cfg.get("limits", "retry_backoff_seconds")
    assert out.status == "FAILED" and "transient" in out.message and "429" in out.message
    assert slept == [backoff * n for n in range(1, retries + 1)]  # linear backoff: 20 s, 40 s
    runs = store.list_runs(tid)
    assert [r["kind"] for r in runs] == ["execute"] + ["retry"] * retries
    assert all(r["classification"] == "transient" for r in runs)
    task = store.get_task(tid)
    assert task.status == TaskStatus.FAILED and task.attempt == retries + 1
    assert review.retry(store, cfg, tid, "claude", "quota reset").status == TaskStatus.READY


def test_no_transient_retry_when_disabled(store, paths, cfg, agy_calc, agy_env):
    registry.set_override(store, cfg, agy_calc.id, "limits", "transient_retries", "0", "user")
    (tid,) = import_tasks(store, cfg, agy_calc, calc_task())
    agy_env([], stderr="HTTP 503 UNAVAILABLE", exit="1")
    slept = []
    assert agy_engine(store, paths, cfg, sleep=slept.append).dispatch(tid).status == "FAILED"
    assert slept == [] and len(store.list_runs(tid)) == 1


def test_a_transient_failure_after_edits_is_still_attributed(store, paths, cfg, calc):
    """Edits made by a run that then failed transiently belong to the executor, even if the retry skips them."""
    (tid,) = import_tasks(store, cfg, calc, calc_task())
    ex = FakeExecutor([
        FakeStep(files={"test_calc.py": "from calc import add\n"}, status=ExecStatus.FAILED,
                 classification=Classification.TRANSIENT, error="429"),
        FakeStep(files={"calc.py": GOOD}, report=DONE),
    ])
    assert engine(store, paths, cfg, ex).dispatch(tid).status == "IN_REVIEW"
    ev = review.build_bundle(store, cfg, tid)["evidence"]
    assert ev["changed_files"] == ["calc.py", "test_calc.py"]


# -- corrupted and partial agy output ---------------------------------------------------

CORRUPTED = {
    "garbage": [b"\x00\xff not json at all", b"<html>502 Bad Gateway</html>"],
    "cut_off_mid_line": [INIT.encode(), agy_result(structured_output=DONE).encode()[:70]],
    "no_result_event": [INIT.encode(), b'{"event":"step_update","step_update":{"step_type":"tool","state":"DONE"}}'],
    "wrong_shapes": [b'{"event":"init","conversation_id":{"x":1}}', b'{"event":"step_update","step_update":"oops"}',
                     b'["an", "array"]', b'{"event":"result","result":"not an object"}'],
    "invalid_utf8": [INIT.encode(), b'{"event":"result","result":{"status":"SUCC\xff\xfeESS"}}'],
}


@pytest.mark.parametrize("name", sorted(CORRUPTED))
def test_corrupted_streams_are_classified_not_raised(agy_env, fake_agy_bin, tmp_path, name):
    agy_env(CORRUPTED[name])
    workdir = tmp_path / "w"
    workdir.mkdir()
    r = AgyExecutor().run(ExecRequest("P1.T1", 1, str(workdir), "do it", report_schema=REPORT_SCHEMA,
                                      timeout_seconds=30))
    assert r.status == ExecStatus.FAILED and r.classification == Classification.EXECUTOR_ERROR, (r.status, r.error)
    assert r.report is None


def test_result_fields_of_the_wrong_type_do_not_crash(agy_env, tmp_path):
    odd = json.dumps({"event": "result", "result": {
        "conversation_id": 42, "status": "SUCCESS", "response": {"text": "hi"}, "structured_output": "DONE",
        "denied_actions": "RunCommand", "usage": {"input_tokens": 5, "output_tokens": float("nan"), "cache": True}}},
        allow_nan=True)
    agy_env([odd])
    workdir = tmp_path / "w"
    workdir.mkdir()
    r = AgyExecutor().run(ExecRequest("P1.T1", 1, str(workdir), "do it", report_schema=REPORT_SCHEMA,
                                      timeout_seconds=30))
    assert r.status == ExecStatus.SUCCEEDED and r.report is None and "no structured report" in r.error
    assert r.usage == {"input_tokens": 5} and r.denied_actions == ["RunCommand"] and r.conversation_id == "42"


def test_corrupted_stream_fails_the_task_instead_of_hanging(store, paths, cfg, agy_calc, agy_env):
    (tid,) = import_tasks(store, cfg, agy_calc, calc_task())
    agy_env(CORRUPTED["cut_off_mid_line"], edits={"calc.py": GOOD})
    out = agy_engine(store, paths, cfg).dispatch(tid)
    assert out.status == "FAILED" and "unreadable stream line" in out.message
    assert store.get_task(tid).status == TaskStatus.FAILED
    assert store.list_runs(tid)[0]["status"] == "failed"


def test_an_adapter_crash_fails_the_run(store, paths, cfg, calc):
    class Broken(FakeExecutor):
        def run(self, request, cancel=None, on_event=None):
            raise RuntimeError("unexpected output shape")

    (tid,) = import_tasks(store, cfg, calc, calc_task())
    out = engine(store, paths, cfg, Broken()).dispatch(tid)
    assert out.status == "FAILED" and "adapter crashed: RuntimeError" in out.message
    assert store.list_runs(tid)[0]["status"] == "failed"
    assert workflow.recover(store, "user") == []  # nothing is left RUNNING


def test_partial_output_with_a_valid_result_still_reaches_review(store, paths, cfg, agy_calc, agy_env):
    """Noise around a valid result line is tolerated: the run is judged on the evidence, as always."""
    (tid,) = import_tasks(store, cfg, agy_calc, calc_task())
    agy_env([b"warning: something odd", INIT.encode(), b'{"event":"step_update"', b"",
             agy_result(structured_output=DONE).encode()], edits={"calc.py": GOOD})
    out = agy_engine(store, paths, cfg).dispatch(tid)
    assert out.status == "IN_REVIEW" and out.checks[0]["passed"]
    assert review.decide(store, cfg, tid, review_doc("APPROVED"), "claude")[0].status == TaskStatus.APPROVED
    assert CALC != Path(agy_calc.root, "calc.py").read_text()
