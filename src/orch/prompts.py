"""Prompts sent to the executor.

Every correction prompt repeats the original task and all earlier review findings,
so requirements and feedback survive any number of cycles, even when the
executor cannot resume its previous conversation.
"""

from __future__ import annotations

from typing import Any

from orch.context import render_task
from orch.executors.base import Capabilities
from orch.models import Task


def _finish(task: Task, caps: Capabilities, checks: list[str]) -> str:
    lines = ["## How to finish", ""]
    if not caps.can_run_commands:
        lines.append("- Shell commands are blocked in this environment. Do not try to run builds or tests; "
                     "edit files only.")
    if checks:
        lines.append(f"- After you finish, the engine itself runs: {', '.join(checks)}. "
                     "Their real output decides whether the work is accepted.")
    lines += [
        f"- Change only paths inside the allowed scope of task {task.id}.",
        "- End with the JSON report: status (DONE, PARTIAL, BLOCKED or NEEDS_CLARIFICATION), files_changed "
        "(paths relative to the repository root), summary, unresolved (anything not done or not verified), "
        "and questions if you need clarification.",
    ]
    return "\n".join(lines)


def initial(context_text: str, task: Task, caps: Capabilities, checks: list[str]) -> str:
    return f"{context_text.strip()}\n\n{_finish(task, caps, checks)}\n"


def _findings_history(reviews: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for n, r in enumerate(reviews, 1):
        data = r["data"]
        out.append(f"### Review {n} ({r['decision']}): {data.get('summary', '')}")
        for f in data.get("findings", []):
            where = f" [{f['file']}{':' + str(f['line']) if f.get('line') else ''}]" if f.get("file") else ""
            out.append(f"- ({f['severity']}, {f['category']}){where} {f['message']}")
            if f.get("instruction"):
                out.append(f"  Fix: {f['instruction']}")
        unmet = [c for c in data.get("criteria", []) if not c.get("met")]
        if unmet:
            out.append("  Unmet criteria: " + ", ".join(c["id"] for c in unmet))
    return "\n".join(out)


def correction(task: Task, reviews: list[dict[str, Any]], caps: Capabilities, checks: list[str]) -> str:
    latest = reviews[-1]["data"] if reviews else {}
    instructions = [f.get("instruction") or f["message"] for f in latest.get("findings", [])]
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(instructions, 1)) or "See the findings above."
    return "\n\n".join([
        f"# Corrections for task {task.id}",
        "Your previous work on this task was reviewed and is not accepted yet. Apply the corrections below. "
        "Keep everything that was already correct; do not start over and do not change anything else.",
        "## The original task (still fully in force)",
        render_task(task),
        "## Review history (oldest first)",
        _findings_history(reviews),
        "## Required corrections",
        numbered,
        _finish(task, caps, checks),
    ]) + "\n"


def auto_fix(task: Task, failing: list[dict[str, Any]], caps: Capabilities, checks: list[str]) -> str:
    blocks = []
    for c in failing:
        state = "timed out" if c.get("timed_out") else f"exit code {c.get('exit_code')}"
        if c.get("skipped"):
            state = f"skipped ({c.get('reason')})"
        blocks.append(f"### `{c['name']}` ({c.get('run')}) failed: {state}\n\n```\n{c.get('output', '').strip()}\n```")
    return "\n\n".join([
        f"# Checks failed for task {task.id}",
        "The engine ran the project's checks on your changes and they failed. Fix the cause. Stay inside the "
        "task's scope; do not weaken, skip or delete tests to make them pass.",
        *blocks,
        "## The task (unchanged)",
        render_task(task),
        _finish(task, caps, checks),
    ]) + "\n"


def denied_retry(task: Task, caps: Capabilities, checks: list[str]) -> str:
    return "\n\n".join([
        f"# Continue task {task.id}",
        "Your last turn tried to use a tool this environment does not allow (for example running a shell command), "
        "so it ended without changes. Complete the task by reading and editing files only. The engine runs the "
        "checks itself afterwards.",
        render_task(task),
        _finish(task, caps, checks),
    ]) + "\n"
