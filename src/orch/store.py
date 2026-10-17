"""Persistent workflow state in SQLite (WAL mode).

- Schema migrations are tracked with PRAGMA user_version.
- Every state change writes an audit event in the same transaction.
- The events table rejects UPDATE and DELETE (append-only, enforced by triggers).
- Task status changes are compare-and-set, so two writers can never both
  move the same task (this is what prevents duplicate dispatch).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orch.errors import NotFoundError, OrchError, TransitionError, ValidationError
from orch.models import Project, ReviewDecision, Task, TaskSpec, TaskStatus
from orch.statemachine import check_transition
from orch.util import dumps, now

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL REFERENCES projects(id),
        title TEXT NOT NULL,
        data TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        plan_id INTEGER NOT NULL REFERENCES plans(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        key TEXT NOT NULL,
        status TEXT NOT NULL,
        review_status TEXT NOT NULL DEFAULT 'NONE',
        attempt INTEGER NOT NULL DEFAULT 0,
        review_cycles INTEGER NOT NULL DEFAULT 0,
        spec TEXT NOT NULL,
        approval_reasons TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (plan_id, key)
    );
    CREATE INDEX tasks_project_status ON tasks (project_id, status);
    CREATE TABLE task_deps (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        depends_on TEXT NOT NULL REFERENCES tasks(id),
        PRIMARY KEY (task_id, depends_on)
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        attempt INTEGER NOT NULL,
        kind TEXT NOT NULL,
        executor TEXT NOT NULL,
        status TEXT NOT NULL,
        conversation_id TEXT,
        pid INTEGER,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        classification TEXT,
        usage TEXT,
        report TEXT,
        transcript_path TEXT,
        error TEXT,
        UNIQUE (task_id, attempt, kind)
    );
    CREATE TABLE evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES runs(id),
        data TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        attempt INTEGER NOT NULL,
        decision TEXT NOT NULL,
        data TEXT NOT NULL,
        reviewer TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        project_id TEXT,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX events_subject ON events (subject_type, subject_id);
    CREATE INDEX events_project ON events (project_id, id);
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TABLE context_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT REFERENCES projects(id),
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        source TEXT NOT NULL,
        source_commit TEXT,
        refs TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'active',
        verified_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        superseded_by INTEGER REFERENCES context_entries(id)
    );
    CREATE INDEX context_project ON context_entries (project_id, status);
    """,
    # v2: snapshots for evidence, and the process that owns a run (for crash recovery)
    """
    ALTER TABLE runs ADD COLUMN before_tree TEXT;
    ALTER TABLE runs ADD COLUMN after_tree TEXT;
    ALTER TABLE runs ADD COLUMN owner_pid INTEGER;
    ALTER TABLE tasks ADD COLUMN baseline_tree TEXT;
    CREATE INDEX runs_status ON runs (status);
    """,
]

_TASK_UPDATABLE = {"attempt", "review_status", "review_cycles", "approval_reasons", "baseline_tree"}
_RUN_UPDATABLE = {
    "status", "conversation_id", "pid", "ended_at", "classification", "usage", "report",
    "transcript_path", "error", "before_tree", "after_tree",
}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._depth = 0
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- infrastructure -------------------------------------------------

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        current = self.schema_version
        if current > len(MIGRATIONS):
            raise OrchError(
                f"database {self.path} is schema v{current}, newer than this orch (v{len(MIGRATIONS)}); upgrade orch"
            )
        for version in range(current, len(MIGRATIONS)):
            try:
                self.conn.executescript(
                    f"BEGIN IMMEDIATE;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. Nested calls join the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    # -- events -----------------------------------------------------------

    def add_event(
        self,
        kind: str,
        subject_type: str,
        subject_id: str,
        actor: str,
        project_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (ts, project_id, subject_type, subject_id, kind, actor, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now(), project_id, subject_type, subject_id, kind, actor, dumps(data or {})),
        )
        return int(cur.lastrowid)

    def list_events(
        self,
        project_id: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("project_id", project_id), ("subject_type", subject_type), ("subject_id", subject_id)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # -- projects ---------------------------------------------------------

    def insert_project(self, project: Project, actor: str) -> Project:
        project.validate()
        ts = now()
        project.created_at = project.updated_at = ts
        with self.tx():
            try:
                self.conn.execute(
                    "INSERT INTO projects (id, data, archived, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (project.id, dumps(project.to_dict()), int(project.archived), ts, ts),
                )
            except sqlite3.IntegrityError as e:
                raise ValidationError(f"project {project.id!r} already exists") from e
            self.add_event("project.added", "project", project.id, actor, project.id, {"root": project.root})
        return project

    def save_project(self, project: Project, actor: str, kind: str, data: dict[str, Any] | None = None) -> Project:
        project.validate()
        project.updated_at = now()
        with self.tx():
            cur = self.conn.execute(
                "UPDATE projects SET data = ?, archived = ?, updated_at = ? WHERE id = ?",
                (dumps(project.to_dict()), int(project.archived), project.updated_at, project.id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"no project {project.id!r}")
            self.add_event(kind, "project", project.id, actor, project.id, data)
        return project

    def get_project(self, project_id: str) -> Project:
        row = self.conn.execute("SELECT data FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no project {project_id!r} (see: orch project list)")
        return Project.from_dict(json.loads(row["data"]))

    def list_projects(self, include_archived: bool = False) -> list[Project]:
        sql = "SELECT data FROM projects" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY id"
        return [Project.from_dict(json.loads(r["data"])) for r in self.conn.execute(sql)]

    def project_exists(self, project_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    # -- plans and tasks --------------------------------------------------

    def create_plan(
        self,
        project_id: str,
        title: str,
        data: dict[str, Any],
        actor: str,
        make_tasks: Callable[[int], list[Task]],
    ) -> tuple[int, list[Task]]:
        """Insert a plan and its tasks atomically. make_tasks receives the new plan id."""
        ts = now()
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO plans (project_id, title, data, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, title, dumps(data), actor, ts),
            )
            plan_id = int(cur.lastrowid)
            tasks = make_tasks(plan_id)
            for t in tasks:
                t.created_at = t.updated_at = ts
                self.conn.execute(
                    "INSERT INTO tasks (id, plan_id, project_id, key, status, review_status, attempt,"
                    " spec, approval_reasons, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        t.id, plan_id, project_id, t.key, t.status.value, t.review_status.value, t.attempt,
                        dumps(t.spec.to_dict()), dumps(t.approval_reasons), ts, ts,
                    ),
                )
            for t in tasks:
                for dep in t.depends_on:
                    self.conn.execute("INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)", (t.id, dep))
            self.add_event(
                "plan.created", "plan", f"P{plan_id}", actor, project_id,
                {"title": title, "tasks": [t.id for t in tasks]},
            )
            for t in tasks:
                self.add_event(
                    "task.created", "task", t.id, actor, project_id,
                    {"status": t.status.value, "approval_reasons": t.approval_reasons},
                )
        return plan_id, tasks

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no plan P{plan_id}")
        return {**dict(row), "data": json.loads(row["data"]), "tasks": self.list_tasks(plan_id=plan_id)}

    def list_plans(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT p.id, p.project_id, p.title, p.created_by, p.created_at,"
            " COUNT(t.id) AS tasks, SUM(t.status = 'APPROVED') AS approved"
            " FROM plans p LEFT JOIN tasks t ON t.plan_id = p.id"
        )
        args: tuple[Any, ...] = ()
        if project_id:
            sql += " WHERE p.project_id = ?"
            args = (project_id,)
        sql += " GROUP BY p.id ORDER BY p.id DESC"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        deps = [
            r["depends_on"]
            for r in self.conn.execute("SELECT depends_on FROM task_deps WHERE task_id = ? ORDER BY 1", (row["id"],))
        ]
        return Task(
            id=row["id"],
            plan_id=row["plan_id"],
            project_id=row["project_id"],
            key=row["key"],
            status=TaskStatus(row["status"]),
            review_status=ReviewDecision(row["review_status"]),
            attempt=row["attempt"],
            spec=TaskSpec.from_dict(json.loads(row["spec"])),
            depends_on=deps,
            approval_reasons=json.loads(row["approval_reasons"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_task(self, task_id: str) -> Task:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no task {task_id!r}")
        return self._row_to_task(row)

    def list_tasks(
        self,
        project_id: str | None = None,
        plan_id: int | None = None,
        statuses: list[TaskStatus] | None = None,
    ) -> list[Task]:
        where, args = [], []
        if project_id:
            where.append("project_id = ?")
            args.append(project_id)
        if plan_id is not None:
            where.append("plan_id = ?")
            args.append(plan_id)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(s.value for s in statuses)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
        return [self._row_to_task(r) for r in self.conn.execute(sql + " ORDER BY plan_id, rowid", args)]

    def transition_task(
        self,
        task_id: str,
        new: TaskStatus,
        actor: str,
        reason: str,
        expected: TaskStatus | None = None,
        updates: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Task:
        updates = dict(updates or {})
        bad = set(updates) - _TASK_UPDATABLE
        if bad:
            raise ValueError(f"cannot update task fields {sorted(bad)}")
        if not reason.strip():
            raise ValidationError("a reason is required for every task status change")
        with self.tx():
            current = self.get_task(task_id)
            if expected is not None and current.status != expected:
                raise TransitionError(
                    f"task {task_id} is {current.status.value}, expected {expected.value} (changed by another writer?)"
                )
            check_transition(current.status, new)
            sets = ["status = ?", "updated_at = ?"]
            args: list[Any] = [new.value, now()]
            for col, val in updates.items():
                sets.append(f"{col} = ?")
                if col == "approval_reasons":
                    val = dumps(val)
                elif col == "review_status":
                    val = ReviewDecision(val).value
                args.append(val)
            cur = self.conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND status = ?",
                (*args, task_id, current.status.value),
            )
            if cur.rowcount != 1:
                raise TransitionError(f"task {task_id} changed concurrently; retry")
            self.add_event(
                "task.status", "task", task_id, actor, current.project_id,
                {"from": current.status.value, "to": new.value, "reason": reason, **(data or {})},
            )
            return self.get_task(task_id)

    def ready_tasks(self, project_id: str | None = None) -> list[Task]:
        """READY tasks whose dependencies are all APPROVED, oldest plan first."""
        sql = (
            "SELECT t.* FROM tasks t WHERE t.status = 'READY' AND NOT EXISTS ("
            " SELECT 1 FROM task_deps d JOIN tasks u ON u.id = d.depends_on"
            " WHERE d.task_id = t.id AND u.status != 'APPROVED')"
        )
        args: tuple[Any, ...] = ()
        if project_id:
            sql += " AND t.project_id = ?"
            args = (project_id,)
        return [self._row_to_task(r) for r in self.conn.execute(sql + " ORDER BY t.plan_id, t.rowid", args)]

    def task_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.conn.execute("SELECT project_id, status, COUNT(*) AS n FROM tasks GROUP BY 1, 2"):
            out.setdefault(r["project_id"], {})[r["status"]] = r["n"]
        return out

    def task_extras(self, task_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT review_cycles, baseline_tree FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no task {task_id!r}")
        return dict(row)

    # -- runs, evidence, reviews --------------------------------------------

    @staticmethod
    def _run_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for key in ("usage", "report"):
            d[key] = json.loads(d[key]) if d.get(key) else None
        return d

    def insert_run(self, task_id: str, attempt: int, kind: str, executor: str, owner_pid: int,
                   conversation_id: str | None = None) -> int:
        with self.tx():
            try:
                cur = self.conn.execute(
                    "INSERT INTO runs (task_id, attempt, kind, executor, status, conversation_id, owner_pid, started_at)"
                    " VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                    (task_id, attempt, kind, executor, conversation_id, owner_pid, now()),
                )
            except sqlite3.IntegrityError as e:
                raise TransitionError(f"run {kind} #{attempt} of {task_id} already exists (duplicate dispatch)") from e
            run_id = int(cur.lastrowid)
            project = self.get_task(task_id).project_id
            self.add_event("run.started", "task", task_id, "engine", project,
                           {"run": run_id, "attempt": attempt, "kind": kind, "executor": executor})
        return run_id

    def update_run(self, run_id: int, **fields: Any) -> None:
        bad = set(fields) - _RUN_UPDATABLE
        if bad:
            raise ValueError(f"cannot update run fields {sorted(bad)}")
        if not fields:
            return
        sets, args = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            args.append(dumps(value) if key in ("usage", "report") and value is not None else value)
        with self.tx():
            self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", (*args, run_id))
            if "status" in fields and fields["status"] != "running":
                row = self.conn.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
                task = self.get_task(row["task_id"])
                self.add_event("run.finished", "task", task.id, "engine", task.project_id,
                               {"run": run_id, "status": fields["status"],
                                "classification": fields.get("classification"), "error": fields.get("error")})

    def get_run(self, run_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no run {run_id}")
        return self._run_row(row)

    def list_runs(self, task_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if task_id:
            where.append("task_id = ?")
            args.append(task_id)
        if status:
            where.append("status = ?")
            args.append(status)
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        return [self._run_row(r) for r in self.conn.execute(sql, args)]

    def insert_evidence(self, run_id: int, data: dict[str, Any]) -> int:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO evidence (run_id, data, created_at) VALUES (?, ?, ?)", (run_id, dumps(data), now())
            )
            return int(cur.lastrowid)

    def latest_evidence(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT e.id, e.run_id, e.data, e.created_at FROM evidence e JOIN runs r ON r.id = e.run_id"
            " WHERE r.task_id = ? ORDER BY e.id DESC LIMIT 1", (task_id,),
        ).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def task_evidence(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.data FROM evidence e JOIN runs r ON r.id = e.run_id WHERE r.task_id = ? ORDER BY e.id",
            (task_id,),
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def evidence_by_run(self, task_id: str) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.run_id, e.data FROM evidence e JOIN runs r ON r.id = e.run_id WHERE r.task_id = ? ORDER BY e.id",
            (task_id,),
        ).fetchall()
        return {r["run_id"]: json.loads(r["data"]) for r in rows}

    def approved_changes(self, project_id: str) -> dict[str, str]:
        """path -> tree id holding the content an APPROVED task left it in (latest approval wins).

        Covers the files the executor changed and every file the engine's checks created
        during any of the task's attempts, at the state recorded by its last evidence.
        """
        rows = self.conn.execute(
            "SELECT r.task_id, e.data FROM evidence e JOIN runs r ON r.id = e.run_id"
            " JOIN tasks t ON t.id = r.task_id WHERE t.project_id = ? AND t.status = 'APPROVED' ORDER BY e.id",
            (project_id,),
        ).fetchall()
        per_task: dict[str, tuple[dict[str, Any], set[str]]] = {}
        order: list[str] = []
        for row in rows:
            data = json.loads(row["data"])
            _, artifacts = per_task.get(row["task_id"], ({}, set()))
            artifacts |= set(data.get("check_artifacts", []))
            if row["task_id"] in per_task:
                order.remove(row["task_id"])
            order.append(row["task_id"])
            per_task[row["task_id"]] = (data, artifacts)
        out: dict[str, str] = {}
        for task_id in order:  # tasks whose latest evidence is newer win
            data, artifacts = per_task[task_id]
            tree = data.get("checks_tree") or data["after"]["tree"]
            for path in set(data.get("changed_files", [])) | artifacts:
                out[path] = tree
        return out

    def approved_files(self, project_id: str) -> dict[str, tuple[str, str]]:
        """path -> (task id, tree with the approved content) for every file an APPROVED task's executor changed.

        Only the executor's changed files count, never check artifacts. When several approved
        tasks changed a file, the task with the newest evidence wins.
        """
        rows = self.conn.execute(
            "SELECT r.task_id, e.data FROM evidence e JOIN runs r ON r.id = e.run_id"
            " JOIN tasks t ON t.id = r.task_id WHERE t.project_id = ? AND t.status = 'APPROVED' ORDER BY e.id",
            (project_id,),
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.pop(row["task_id"], None)  # re-insert, so the order is by each task's newest evidence
            latest[row["task_id"]] = json.loads(row["data"])
        out: dict[str, tuple[str, str]] = {}
        for task_id, data in latest.items():
            tree = data.get("checks_tree") or data["after"]["tree"]
            for path in data.get("changed_files", []):
                out[path] = (task_id, tree)
        return out

    def insert_review(self, task_id: str, attempt: int, decision: str, data: dict[str, Any], reviewer: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO reviews (task_id, attempt, decision, data, reviewer, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, attempt, decision, dumps(data), reviewer, now()),
        )
        return int(cur.lastrowid)

    def list_reviews(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM reviews WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # -- context ----------------------------------------------------------

    def add_context_entry(
        self,
        project_id: str | None,
        kind: str,
        title: str,
        body: str,
        source: str,
        actor: str,
        refs: list[str] | None = None,
        source_commit: str | None = None,
    ) -> int:
        ts = now()
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO context_entries (project_id, kind, title, body, source, source_commit, refs,"
                " verified_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, kind, title, body, source, source_commit, dumps(refs or []), ts, ts),
            )
            entry_id = int(cur.lastrowid)
            self.add_event(
                "context.added", "context", str(entry_id), actor, project_id,
                {"kind": kind, "title": title, "source": source},
            )
        return entry_id

    def list_context_entries(
        self,
        project_id: str | None,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        where = ["project_id IS ?"]
        args: list[Any] = [project_id]
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        rows = self.conn.execute(
            f"SELECT * FROM context_entries WHERE {' AND '.join(where)} ORDER BY id", args
        ).fetchall()
        return [{**dict(r), "refs": json.loads(r["refs"])} for r in rows]

    def set_context_status(
        self,
        entry_id: int,
        status: str,
        actor: str,
        reason: str,
        superseded_by: int | None = None,
        reverified_at_commit: str | None = None,
    ) -> None:
        """Change an entry's status. reverified_at_commit records a fresh verification at that commit."""
        if status not in ("active", "stale", "superseded"):
            raise ValidationError(f"unknown context status {status!r}")
        with self.tx():
            row = self.conn.execute("SELECT project_id FROM context_entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"no context entry {entry_id}")
            sets, args = ["status = ?", "superseded_by = ?"], [status, superseded_by]
            if reverified_at_commit is not None:
                sets += ["verified_at = ?", "source_commit = ?"]
                args += [now(), reverified_at_commit]
            self.conn.execute(f"UPDATE context_entries SET {', '.join(sets)} WHERE id = ?", (*args, entry_id))
            self.add_event(
                "context.status", "context", str(entry_id), actor, row["project_id"],
                {"status": status, "reason": reason},
            )
