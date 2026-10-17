"""Versioned data contracts and a small JSON Schema validator.

The validator covers the subset of JSON Schema these contracts use: type,
enum, properties, required, additionalProperties (bool), items, minItems,
minLength, maxLength, pattern, minimum, maximum. It returns every error with
a JSON path, so a model can fix all of them in one pass.
"""

from __future__ import annotations

import re
from typing import Any

COMMAND_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
TASK_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,23}$"

_STR: dict[str, Any] = {"type": "string", "minLength": 1}
_STR_LIST: dict[str, Any] = {"type": "array", "items": _STR}

ACCEPTANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "text", "verify"],
    "properties": {
        "id": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,16}$"},
        "text": _STR,
        "verify": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind"],
            "properties": {
                "kind": {"enum": ["command", "review"]},
                "command": {"type": "string", "pattern": COMMAND_NAME_PATTERN},
            },
        },
    },
}

TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "title", "objective", "scope", "instructions", "acceptance", "risk"],
    "properties": {
        "key": {"type": "string", "pattern": TASK_KEY_PATTERN},
        "title": _STR,
        "objective": _STR,
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["allow"],
            "properties": {
                "allow": {"type": "array", "minItems": 1, "items": _STR},
                "deny": _STR_LIST,
            },
        },
        "relevant_files": _STR_LIST,
        "instructions": _STR,
        "acceptance": {"type": "array", "minItems": 1, "items": ACCEPTANCE_SCHEMA},
        "required_checks": {"type": "array", "items": {"type": "string", "pattern": COMMAND_NAME_PATTERN}},
        "risk": {"enum": ["low", "medium", "high"]},
        "depends_on": _STR_LIST,
        "deliverables": _STR_LIST,
        "model": _STR,
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "$id": "orch/plan/v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "project_id", "title", "objective", "tasks"],
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "project_id": _STR,
        "title": _STR,
        "objective": _STR,
        "context_summary": {"type": "string"},
        "decision": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {
                "summary": _STR,
                "rationale": {"type": "string"},
                "tradeoffs": _STR_LIST,
                "architectural": {"type": "boolean"},
            },
        },
        "risks": _STR_LIST,
        "unknowns": _STR_LIST,
        "tasks": {"type": "array", "minItems": 1, "items": TASK_SCHEMA},
    },
}

# What an executor must return at the end of every run.
REPORT_SCHEMA: dict[str, Any] = {
    "$id": "orch/executor-report/v1",
    "type": "object",
    "required": ["status", "files_changed", "summary", "unresolved"],
    "properties": {
        "status": {"enum": ["DONE", "PARTIAL", "BLOCKED", "NEEDS_CLARIFICATION"]},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

# What Claude records when it reviews a task's evidence.
REVIEW_SCHEMA: dict[str, Any] = {
    "$id": "orch/review/v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "summary", "findings", "criteria"],
    "properties": {
        "decision": {"enum": ["APPROVED", "NEEDS_CORRECTION", "BLOCKED", "NEEDS_HUMAN_APPROVAL"]},
        "summary": _STR,
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "category", "message"],
                "properties": {
                    "severity": {"enum": ["blocker", "major", "minor", "nit"]},
                    "category": {
                        "enum": [
                            "correctness", "acceptance", "architecture", "quality", "security",
                            "error_handling", "performance", "compatibility", "regression",
                            "tests", "scope", "other",
                        ]
                    },
                    "file": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "message": _STR,
                    "instruction": {"type": "string"},
                },
            },
        },
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "met"],
                "properties": {"id": _STR, "met": {"type": "boolean"}, "evidence": {"type": "string"}},
            },
        },
    },
}

SCHEMAS = {"plan": PLAN_SCHEMA, "report": REPORT_SCHEMA, "review": REVIEW_SCHEMA}

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_ok(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, n) for n in names):
            return [f"{path}: expected {' or '.join(names)}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}, got {instance!r}")
    if isinstance(instance, str):
        if len(instance.strip()) < schema.get("minLength", 0):
            errors.append(f"{path}: must not be empty")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}.{key}: required")
        for key, value in instance.items():
            if key in props:
                errors.extend(validate(value, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}.{key}: unknown field")
    return errors
