import sqlite3
import threading

import pytest

from orch import plans
from orch.errors import OrchError, TransitionError, ValidationError
from orch.models import TaskStatus
from orch.statemachine import TRANSITIONS, can_transition
from orch.store import MIGRATIONS, Store

from conftest import plan_dict, task_dict


def test_migrations_are_idempotent(paths):
    Store(paths.db_file).close()
    s = Store(paths.db_file)
    assert s.schema_version == len(MIGRATIONS)
    s.close()


def test_refuses_newer_database(paths):
    conn = sqlite3.connect(paths.db_file)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(OrchError, match="newer than this orch"):
        Store(paths.db_file)


def test_events_are_append_only(store):
    store.add_event("x", "test", "1", "user")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE events SET kind = 'y'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events")


def test_every_status_has_a_transition_entry():
    assert set(TRANSITIONS) == set(TaskStatus)
    assert not TRANSITIONS[TaskStatus.CANCELLED]
    assert can_transition(TaskStatus.VERIFYING, TaskStatus.READY)
    assert not can_transition(TaskStatus.READY, TaskStatus.APPROVED)


def test_transition_records_event_and_rejects_illegal_moves(store, cfg, project):
    result = plans.import_plan(store, cfg, plan_dict(project.id), "claude")
    tid = result.tasks[0].id
    t = store.transition_task(tid, TaskStatus.RUNNING, "engine", "dispatch", expected=TaskStatus.READY,
                              updates={"attempt": 1})
    assert t.status == TaskStatus.RUNNING and t.attempt == 1
    with pytest.raises(TransitionError, match="illegal transition"):
        store.transition_task(tid, TaskStatus.APPROVED, "claude", "skip review")
    with pytest.raises(TransitionError, match="expected READY"):
        store.transition_task(tid, TaskStatus.EXECUTED, "engine", "x", expected=TaskStatus.READY)
    with pytest.raises(ValidationError, match="reason"):
        store.transition_task(tid, TaskStatus.EXECUTED, "engine", "  ")
    with pytest.raises(ValueError):
        store.transition_task(tid, TaskStatus.EXECUTED, "engine", "x", updates={"status": "APPROVED"})
    kinds = [(e["data"].get("from"), e["data"].get("to")) for e in store.list_events(subject_type="task", subject_id=tid)]
    assert ("READY", "RUNNING") in kinds
    assert store.get_task(tid).status == TaskStatus.RUNNING  # failed attempts left no trace


def test_compare_and_set_lets_only_one_dispatcher_win(paths, cfg, project, store):
    tid = plans.import_plan(store, cfg, plan_dict(project.id), "claude").tasks[0].id
    winners, losers = [], []
    barrier = threading.Barrier(4)

    def dispatch() -> None:
        s = Store(paths.db_file)
        barrier.wait()
        try:
            s.transition_task(tid, TaskStatus.RUNNING, "engine", "dispatch", expected=TaskStatus.READY)
            winners.append(1)
        except TransitionError:
            losers.append(1)
        finally:
            s.close()

    threads = [threading.Thread(target=dispatch) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1 and len(losers) == 3


def test_ready_tasks_respect_dependencies(store, cfg, project):
    tasks = [task_dict("T1"), task_dict("T2", depends_on=["T1"])]
    result = plans.import_plan(store, cfg, plan_dict(project.id, tasks), "claude")
    t1, t2 = (t.id for t in result.tasks)
    assert [t.id for t in store.ready_tasks()] == [t1]
    for status in (TaskStatus.RUNNING, TaskStatus.EXECUTED, TaskStatus.VERIFYING, TaskStatus.IN_REVIEW,
                   TaskStatus.APPROVED):
        store.transition_task(t1, status, "engine", "advance")
    assert [t.id for t in store.ready_tasks(project.id)] == [t2]


def test_plan_insert_is_atomic(store, project):
    def boom(plan_id):
        raise RuntimeError("fail midway")

    with pytest.raises(RuntimeError):
        store.create_plan(project.id, "x", {}, "claude", boom)
    assert store.list_plans() == []
