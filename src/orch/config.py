"""Configuration: built-in defaults, ~/.config/orch/config.toml, and per-project overrides.

Unknown keys and wrong types are errors rather than warnings, so a typo in a
safety setting can never silently fall back to a default.
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import globs
from orch.errors import ConfigError

KNOWN_EXECUTORS = frozenset({"agy", "manual", "fake"})

# Every action the engine can take that a policy decides on.
ACTIONS: dict[str, str] = {
    "inspect": "Read files, run discovery, query git (read-only).",
    "edit_in_scope": "Executor edits files inside the task's declared scope.",
    "run_verification": "Run the project's confirmed build/test/lint commands.",
    "start_services": "Start project services (e.g. docker compose up) needed by verification.",
    "architectural_decision": "Execute a plan that records an architectural decision.",
    "unbounded_scope": "Dispatch a task whose scope is the whole repository.",
    "dependency_change": "Change dependency manifests or lockfiles.",
    "migration": "Add or change database migrations.",
    "sensitive_file": "Touch files matching the sensitive globs (secrets, CI, infra).",
    "large_deletion": "Delete more lines than large_deletion_lines.",
    "delete_files": "Delete tracked files.",
    "enable_external_engine": "Allow an external engine (sends code off this machine) for a project.",
    "git_branch": "Create or switch to a working branch.",
    "git_commit": "Create a commit.",
    "git_push": "Push a branch to a remote.",
    "merge_request": "Open a merge/pull request.",
    "merge": "Merge a branch.",
    "destructive_migration": "Run a migration that drops or rewrites data.",
    "secrets_change": "Change secrets, credentials or security policy.",
    "production_infrastructure": "Change production infrastructure.",
    "deploy_production": "Deploy to production.",
    "force_push": "Force-push (rewrites remote history).",
    "merge_protected_branch": "Merge into a protected branch.",
    "discard_user_changes": "Discard uncommitted changes the user made.",
}

# These can never be configured as automatic, whatever the config says.
NEVER_AUTOMATIC = frozenset(
    {
        "enable_external_engine",
        "destructive_migration",
        "secrets_change",
        "production_infrastructure",
        "deploy_production",
        "force_push",
        "merge_protected_branch",
        "discard_user_changes",
    }
)

_DEFAULT_AUTO = ["inspect", "edit_in_scope", "run_verification", "git_branch"]

DEFAULTS: dict[str, dict[str, Any]] = {
    "executor": {
        "default": "agy",
        "model": "gemini-3.8-flash-high",
    },
    "limits": {
        "auto_fix_attempts": 2,
        "max_review_cycles": 3,
        "per_repo_concurrency": 1,
        "global_concurrency": 2,
        "run_timeout_seconds": 1800,
        "evidence_output_bytes": 20000,
        "context_budget_chars": 24000,
        "transient_retries": 2,
        "retry_backoff_seconds": 20,
    },
    "policy": {
        "require_approval_for_risk": ["high"],
        "auto_actions": list(_DEFAULT_AUTO),
        "approval_actions": [a for a in ACTIONS if a not in _DEFAULT_AUTO],
        "protected_branches": ["main", "master", "production", "prod", "release/*"],
        "sensitive_globs": [
            ".env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*", "secrets/", "credentials*",
            ".gitlab-ci.yml", ".github/workflows/", "docker-compose*.prod*.yml", "infra/", "deploy/",
            "k8s/", "terraform/", "auth.json",
        ],
        "dependency_globs": [
            "composer.json", "composer.lock", "package.json", "package-lock.json", "yarn.lock",
            "pnpm-lock.yaml", "bun.lockb", "go.mod", "go.sum", "pyproject.toml", "poetry.lock",
            "uv.lock", "requirements*.txt", "Pipfile", "Pipfile.lock", "pubspec.yaml", "pubspec.lock",
            "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
        ],
        "migration_globs": ["migrations/", "database/migrations/", "db/migrate/", "alembic/versions/"],
        "large_deletion_lines": 200,
    },
}

_MIN: dict[tuple[str, str], int] = {
    ("limits", "auto_fix_attempts"): 0,
    ("limits", "max_review_cycles"): 1,
    ("limits", "per_repo_concurrency"): 1,
    ("limits", "global_concurrency"): 1,
    ("limits", "run_timeout_seconds"): 1,
    ("limits", "evidence_output_bytes"): 1000,
    ("limits", "context_budget_chars"): 1000,
    ("limits", "transient_retries"): 0,
    ("limits", "retry_backoff_seconds"): 0,
    ("policy", "large_deletion_lines"): 1,
}


def _type_name(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list of strings"
    return type(v).__name__


def _check_types(layer: dict[str, Any], where: str) -> list[str]:
    errors = []
    for section, values in layer.items():
        if section not in DEFAULTS:
            errors.append(f"{where}: unknown section [{section}]")
            continue
        if not isinstance(values, dict):
            errors.append(f"{where}: [{section}] must be a table")
            continue
        for key, value in values.items():
            if key not in DEFAULTS[section]:
                errors.append(f"{where}: unknown key {section}.{key}")
                continue
            want = _type_name(DEFAULTS[section][key])
            got = _type_name(value)
            if want != got or (isinstance(value, list) and not all(isinstance(x, str) for x in value)):
                errors.append(f"{where}: {section}.{key} must be of type {want}, got {got}")
    return errors


def _merge_layer(base: dict[str, Any], layer: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for section, values in layer.items():
        out.setdefault(section, {}).update(copy.deepcopy(values))
        if section == "policy":
            # Giving only one of the two action lists means "everything else is in the other one".
            has_auto, has_approval = "auto_actions" in values, "approval_actions" in values
            if has_auto and not has_approval:
                out["policy"]["approval_actions"] = [a for a in ACTIONS if a not in values["auto_actions"]]
            elif has_approval and not has_auto:
                out["policy"]["auto_actions"] = [a for a in ACTIONS if a not in values["approval_actions"]]
    return out


def _check_semantics(data: dict[str, Any], where: str) -> list[str]:
    errors = []
    for (section, key), minimum in _MIN.items():
        if data[section][key] < minimum:
            errors.append(f"{where}: {section}.{key} must be >= {minimum}")
    if data["executor"]["default"] not in KNOWN_EXECUTORS:
        errors.append(f"{where}: executor.default must be one of {sorted(KNOWN_EXECUTORS)}")
    policy = data["policy"]
    auto, approval = set(policy["auto_actions"]), set(policy["approval_actions"])
    for name in sorted((auto | approval) - set(ACTIONS)):
        errors.append(f"{where}: unknown action {name!r}")
    for name in sorted(auto & approval):
        errors.append(f"{where}: action {name!r} is in both auto_actions and approval_actions")
    for name in sorted(set(ACTIONS) - auto - approval):
        errors.append(f"{where}: action {name!r} is in neither auto_actions nor approval_actions")
    for name in sorted(auto & NEVER_AUTOMATIC):
        errors.append(f"{where}: action {name!r} can never be automatic")
    bad_risk = set(policy["require_approval_for_risk"]) - {"low", "medium", "high"}
    if bad_risk:
        errors.append(f"{where}: require_approval_for_risk has unknown levels {sorted(bad_risk)}")
    for key in ("sensitive_globs", "dependency_globs", "migration_globs"):
        for pattern in policy[key]:
            problem = globs.validate_pattern(pattern)
            if problem:
                errors.append(f"{where}: policy.{key}: {problem}")
    return errors


@dataclass(frozen=True)
class Config:
    data: dict[str, Any]
    source: Path | None = None

    def get(self, section: str, key: str) -> Any:
        return self.data[section][key]

    def requires_approval(self, action: str) -> bool:
        if action not in ACTIONS:
            raise KeyError(f"unknown action {action!r}")
        return action in NEVER_AUTOMATIC or action not in self.data["policy"]["auto_actions"]

    def with_overrides(self, overrides: dict[str, Any], where: str) -> Config:
        errors = _check_types(overrides, where)
        if errors:
            raise ConfigError("invalid project overrides", errors)
        data = _merge_layer(self.data, overrides)
        errors = _check_semantics(data, where)
        if errors:
            raise ConfigError("invalid project overrides", errors)
        return Config(data=data, source=self.source)


def build_config(layer: dict[str, Any] | None = None, where: str = "config") -> Config:
    layer = layer or {}
    errors = _check_types(layer, where)
    if errors:
        raise ConfigError(f"invalid configuration ({where})", errors)
    data = _merge_layer(DEFAULTS, layer)
    errors = _check_semantics(data, where)
    if errors:
        raise ConfigError(f"invalid configuration ({where})", errors)
    return Config(data=data)


def load_config(path: Path) -> Config:
    if not path.exists():
        return build_config()
    try:
        layer = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"cannot parse {path}", [str(e)]) from e
    config = build_config(layer, where=str(path))
    return Config(data=config.data, source=path)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[\n" + "".join(f"  {_toml_value(x)},\n" for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def default_config_text() -> str:
    lines = [
        "# orch configuration. Every key is optional; omitted keys use these defaults.",
        "# Projects can override [executor], [limits] and [policy] individually.",
        "",
    ]
    for section, values in DEFAULTS.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    lines.append("# Actions that can never be automatic: " + ", ".join(sorted(NEVER_AUTOMATIC)))
    return "\n".join(lines) + "\n"
