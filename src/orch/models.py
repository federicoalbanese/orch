"""Domain models. Every model round-trips through plain dicts (stored as JSON)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from orch.errors import ValidationError
from orch.schema import COMMAND_NAME_PATTERN

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
COMMAND_NAME_RE = re.compile(COMMAND_NAME_PATTERN)


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(str, Enum):
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY = "READY"
    RUNNING = "RUNNING"
    EXECUTED = "EXECUTED"
    VERIFYING = "VERIFYING"
    IN_REVIEW = "IN_REVIEW"
    NEEDS_CORRECTION = "NEEDS_CORRECTION"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN_APPROVAL = "NEEDS_HUMAN_APPROVAL"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReviewDecision(str, Enum):
    NONE = "NONE"
    APPROVED = "APPROVED"
    NEEDS_CORRECTION = "NEEDS_CORRECTION"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN_APPROVAL = "NEEDS_HUMAN_APPROVAL"


class Actor(str, Enum):
    USER = "user"
    CLAUDE = "claude"
    ENGINE = "engine"


@dataclass
class Command:
    """A project command the engine may run (build, test, lint, ...).

    Only confirmed commands are ever executed; discovered ones start unconfirmed.
    """

    run: str
    cwd: str = "."
    timeout_seconds: int = 900
    requires_services: bool = False
    source: str = "user"
    confidence: str = "high"
    confirmed: bool = False
    notes: str = ""

    def validate(self, name: str) -> list[str]:
        errors = []
        if not COMMAND_NAME_RE.match(name):
            errors.append(f"command name {name!r} must match {COMMAND_NAME_PATTERN}")
        if not self.run.strip():
            errors.append(f"command {name!r}: 'run' is empty")
        if self.cwd.startswith("/") or ".." in self.cwd.split("/"):
            errors.append(f"command {name!r}: cwd must be relative to the project root")
        if self.timeout_seconds <= 0:
            errors.append(f"command {name!r}: timeout_seconds must be positive")
        if self.confidence not in ("high", "medium", "low"):
            errors.append(f"command {name!r}: confidence must be high, medium or low")
        return errors

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Command:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Project:
    id: str
    name: str
    root: str
    description: str = ""
    remote: str | None = None
    default_branch: str | None = None
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    allow_external_engine: bool = False
    executor: str | None = None
    model: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""

    def validate(self) -> None:
        errors = []
        if not PROJECT_ID_RE.match(self.id):
            errors.append(f"project id {self.id!r} must be lowercase letters, digits and dashes")
        if not self.name.strip():
            errors.append("project name is empty")
        if not self.root.startswith("/"):
            errors.append("project root must be an absolute path")
        for name, cmd in self.commands.items():
            errors.extend(cmd.validate(name))
        unknown = set(self.overrides) - {"limits", "policy", "executor"}
        if unknown:
            errors.append(f"unknown override sections: {sorted(unknown)}")
        if errors:
            raise ValidationError(f"invalid project {self.id!r}", errors)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["commands"] = {k: asdict(v) for k, v in self.commands.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Project:
        data = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        data["commands"] = {k: Command.from_dict(v) for k, v in (d.get("commands") or {}).items()}
        return cls(**data)


@dataclass
class Verify:
    kind: str
    command: str | None = None


@dataclass
class Acceptance:
    id: str
    text: str
    verify: Verify


@dataclass
class Scope:
    allow: list[str]
    deny: list[str] = field(default_factory=list)


@dataclass
class TaskSpec:
    key: str
    title: str
    objective: str
    scope: Scope
    instructions: str
    acceptance: list[Acceptance]
    risk: Risk
    relevant_files: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk"] = self.risk.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskSpec:
        """Build from a dict that already passed TASK_SCHEMA validation."""
        return cls(
            key=d["key"],
            title=d["title"],
            objective=d["objective"],
            scope=Scope(allow=list(d["scope"]["allow"]), deny=list(d["scope"].get("deny", []))),
            instructions=d["instructions"],
            acceptance=[
                Acceptance(id=a["id"], text=a["text"], verify=Verify(**a["verify"])) for a in d["acceptance"]
            ],
            risk=Risk(d["risk"]),
            relevant_files=list(d.get("relevant_files", [])),
            required_checks=list(d.get("required_checks", [])),
            depends_on=list(d.get("depends_on", [])),
            deliverables=list(d.get("deliverables", [])),
            model=d.get("model"),
        )


@dataclass
class Task:
    id: str
    plan_id: int
    project_id: str
    key: str
    status: TaskStatus
    review_status: ReviewDecision
    attempt: int
    spec: TaskSpec
    depends_on: list[str] = field(default_factory=list)
    approval_reasons: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "key": self.key,
            "status": self.status.value,
            "review_status": self.review_status.value,
            "attempt": self.attempt,
            "depends_on": self.depends_on,
            "approval_reasons": self.approval_reasons,
            "spec": self.spec.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def task_id(plan_id: int, key: str) -> str:
    return f"P{plan_id}.{key}"
