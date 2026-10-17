"""Legal task status transitions (docs/architecture.md, section 3.4)."""

from __future__ import annotations

from orch.errors import TransitionError
from orch.models import TaskStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.PLANNED: frozenset({S.AWAITING_APPROVAL, S.READY, S.CANCELLED}),
    S.AWAITING_APPROVAL: frozenset({S.READY, S.CANCELLED}),
    S.READY: frozenset({S.RUNNING, S.AWAITING_APPROVAL, S.CANCELLED}),
    S.RUNNING: frozenset({S.EXECUTED, S.INTERRUPTED, S.FAILED, S.CANCELLED}),
    S.EXECUTED: frozenset({S.VERIFYING, S.INTERRUPTED, S.FAILED, S.CANCELLED}),
    # VERIFYING -> READY is the automatic fix loop (a failed check goes straight back to the executor).
    S.VERIFYING: frozenset({S.IN_REVIEW, S.READY, S.INTERRUPTED, S.FAILED, S.CANCELLED}),
    S.IN_REVIEW: frozenset({S.APPROVED, S.NEEDS_CORRECTION, S.BLOCKED, S.NEEDS_HUMAN_APPROVAL, S.CANCELLED}),
    S.NEEDS_CORRECTION: frozenset({S.READY, S.FAILED, S.CANCELLED}),
    S.NEEDS_HUMAN_APPROVAL: frozenset({S.APPROVED, S.READY, S.BLOCKED, S.CANCELLED}),
    S.BLOCKED: frozenset({S.READY, S.CANCELLED}),
    S.INTERRUPTED: frozenset({S.READY, S.FAILED, S.CANCELLED}),
    # Reopening finished work needs an explicit reason (enforced by the caller).
    S.APPROVED: frozenset({S.READY}),
    S.FAILED: frozenset({S.READY, S.CANCELLED}),
    S.CANCELLED: frozenset(),
}

ACTIVE = frozenset({S.RUNNING, S.EXECUTED, S.VERIFYING})
WAITING_ON_HUMAN = frozenset({S.AWAITING_APPROVAL, S.NEEDS_HUMAN_APPROVAL, S.BLOCKED, S.FAILED})
DONE = frozenset({S.APPROVED, S.CANCELLED})


def can_transition(old: S, new: S) -> bool:
    return new in TRANSITIONS[old]


def check_transition(old: S, new: S) -> None:
    if not can_transition(old, new):
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS[old])) or "none (terminal)"
        raise TransitionError(f"illegal transition {old.value} -> {new.value}; allowed: {allowed}")
