"""Inspect a repository before anything is proposed for it.

Discovery is strictly read-only: it reads a few top-level files and runs
read-only git queries. It never runs project commands. It proposes commands
(test, lint, build, ...) with a source and a confidence; nothing proposed is
trusted until a user confirms it.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orch import gitutil
from orch.errors import ValidationError
from orch.models import Command
from orch.util import slugify

Which = Callable[[str], "str | None"]

MAX_READ = 2 * 1024 * 1024
COMPOSE_DEFAULTS = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
COMPOSE_GLOBS = ("compose*.y*ml", "docker-compose*.y*ml")
APP_SERVICE_RE = re.compile(r"(app|php|laravel|api|backend|fpm|web|server|workspace)", re.I)
NON_APP_SERVICE_RE = re.compile(r"(nginx|mysql|maria|postgres|pgsql|redis|mongo|db|cache|queue|mail|elastic|kibana|minio|adminer|phpmyadmin)", re.I)

MAKE_TARGETS: dict[str, tuple[str, ...]] = {
    "test": ("test", "tests", "check", "test-all", "phpunit", "pest"),
    "lint": ("lint", "lint-check", "cs", "phpcs", "pint", "style"),
    "typecheck": ("typecheck", "type-check", "analyse", "analyze", "stan", "phpstan", "mypy"),
    "build": ("build",),
    "format": ("fmt", "format"),
}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Candidate:
    run: str
    source: str
    confidence: str
    requires_services: bool = False
    notes: str = ""
    priority: int = 5

    def to_command(self) -> Command:
        return Command(
            run=self.run,
            requires_services=self.requires_services,
            source=f"discovered:{self.source}",
            confidence=self.confidence,
            confirmed=False,
            notes=self.notes,
        )


@dataclass
class DiscoveryReport:
    root: str
    suggested_id: str
    suggested_name: str
    is_git: bool = False
    remote: str | None = None
    default_branch: str | None = None
    current_branch: str | None = None
    head: str | None = None
    dirty: bool | None = None
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    makefile_targets: list[str] = field(default_factory=list)
    compose_files: list[str] = field(default_factory=list)
    compose_services: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    candidates: dict[str, list[Candidate]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def proposed_commands(self) -> dict[str, Command]:
        return {name: cands[0].to_command() for name, cands in self.candidates.items() if cands}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for cands in d["candidates"].values():
            for c in cands:
                c.pop("priority", None)
        return d


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_READ:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_makefile(text: str) -> dict[str, list[str]]:
    """Targets and their recipe lines (a heuristic parser: no includes or variable expansion)."""
    targets: dict[str, list[str]] = {}
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("\t"):
            for name in current:
                targets[name].append(line.strip())
            continue
        m = re.match(r"^([^\s:=#][^:=#]*?)\s*:(?![:=])", line)
        if m and "$" not in m.group(1):
            names = [n for n in m.group(1).split() if not n.startswith(".") and "%" not in n]
            for n in names:
                targets.setdefault(n, [])
            current = names
        elif line.strip() and not line.lstrip().startswith("#"):
            current = []
    return targets


def parse_compose_services(text: str) -> list[str]:
    """Top-level service names from a compose file (indentation-based, no YAML library needed)."""
    services: list[str] = []
    in_services, indent = False, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^services:\s*(#.*)?$", line):
            in_services, indent = True, None
            continue
        if in_services:
            if not line[0].isspace():
                in_services = False
                continue
            m = re.match(r"^(\s+)([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
            if m:
                width = len(m.group(1))
                if indent is None:
                    indent = width
                if width == indent:
                    services.append(m.group(2))
    return services


class _Discovery:
    def __init__(self, root: Path, which: Which):
        self.root = root
        self.which = which
        self.r = DiscoveryReport(root=str(root), suggested_id=slugify(root.name), suggested_name=root.name)

    # -- helpers ----------------------------------------------------------

    def exists(self, name: str) -> bool:
        return (self.root / name).exists()

    def add(self, items: list[str], *values: str) -> None:
        for v in values:
            if v not in items:
                items.append(v)

    def candidate(self, name: str, run: str, source: str, confidence: str = "high",
                  requires_services: bool = False, notes: str = "", priority: int = 5) -> None:
        cands = self.r.candidates.setdefault(name, [])
        if any(c.run == run for c in cands):
            return
        cands.append(Candidate(run, source, confidence, requires_services, notes, priority))

    def host_has(self, run: str) -> tuple[bool, str]:
        try:
            first = shlex.split(run)[0]
        except (ValueError, IndexError):
            return False, run
        if first.startswith("vendor/"):
            return self.which("php") is not None, "php"
        if first.startswith("./"):
            return (self.root / first).exists(), first
        return self.which(first) is not None, first

    def read_json(self, name: str) -> dict[str, Any] | None:
        text = _read_text(self.root / name)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            self.r.warnings.append(f"{name}: invalid JSON ({e.msg})")
            return None
        return data if isinstance(data, dict) else None

    def read_toml(self, name: str) -> dict[str, Any] | None:
        text = _read_text(self.root / name)
        if text is None:
            return None
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            self.r.warnings.append(f"{name}: invalid TOML ({e})")
            return None

    # -- detectors --------------------------------------------------------

    def git(self) -> None:
        r = self.r
        r.is_git = gitutil.is_repo(self.root)
        if not r.is_git:
            r.warnings.append("not a git repository: diffs, snapshots and scope checks will be unavailable")
            return
        top = gitutil.toplevel(self.root)
        if top and top.resolve() != self.root:
            r.warnings.append(f"inside a larger git repository rooted at {top}")
        r.remote = gitutil.remote_url(self.root)
        r.default_branch = gitutil.default_branch(self.root)
        r.current_branch = gitutil.current_branch(self.root)
        r.head = gitutil.head_commit(self.root)
        r.dirty = gitutil.is_dirty(self.root)

    def layout(self) -> None:
        r = self.r
        for name in sorted(p.name for p in self.root.iterdir() if not p.name.startswith(".git")):
            low = name.lower()
            if low.startswith(("readme", "contributing", "architecture")) or name in (
                "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", "docs",
            ):
                r.docs.append(name)
            if name in ("tests", "test", "spec", "__tests__", "e2e"):
                r.test_dirs.append(name)
        compose = sorted({p.name for g in COMPOSE_GLOBS for p in self.root.glob(g) if p.is_file()})
        r.compose_files = compose
        main = next((c for c in COMPOSE_DEFAULTS if c in compose), compose[0] if compose else None)
        if main:
            r.compose_services = parse_compose_services(_read_text(self.root / main) or "")
        if self.exists("Dockerfile"):
            r.markers.append("Dockerfile")
        r.markers.extend(compose)

    def compose_exec(self, inner: str) -> str | None:
        """Wrap a command to run inside the most likely application container."""
        services = self.r.compose_services
        app = [s for s in services if APP_SERVICE_RE.search(s) and not NON_APP_SERVICE_RE.search(s)]
        if not app:
            return None
        files = self.r.compose_files
        default = next((c for c in COMPOSE_DEFAULTS if c in files), None)
        flag = "" if default else f"-f {shlex.quote(files[0])} "
        return f"docker compose {flag}exec -T {app[0]} {inner}"

    def makefile(self) -> None:
        name = next((n for n in ("Makefile", "makefile", "GNUmakefile") if self.exists(n)), None)
        if not name:
            return
        self.r.markers.append(name)
        targets = parse_makefile(_read_text(self.root / name) or "")
        self.r.makefile_targets = sorted(targets)
        for cmd, options in MAKE_TARGETS.items():
            for target in options:
                if target in targets:
                    recipe = " ".join(targets[target])
                    services = bool(re.search(r"\bdocker\b|\bcompose\b|\bsail\b", recipe))
                    self.candidate(cmd, f"make {target}", f"{name}:{target}", "high", services, priority=0)
                    break

    def php(self) -> None:
        data = self.read_json("composer.json")
        if data is None and not self.exists("artisan"):
            return
        r = self.r
        self.add(r.languages, "php")
        self.add(r.package_managers, "composer")
        r.markers.append("composer.json")
        require = {**(data or {}).get("require", {}), **(data or {}).get("require-dev", {})}
        laravel = self.exists("artisan") or "laravel/framework" in require
        if laravel:
            self.add(r.frameworks, "laravel")
        if "symfony/framework-bundle" in require:
            self.add(r.frameworks, "symfony")
        if "laravel/lumen-framework" in require:
            self.add(r.frameworks, "lumen")

        native: list[tuple[str, str, str]] = []
        if laravel:
            native.append(("test", "php artisan test", "artisan"))
        if "pestphp/pest" in require:
            native.append(("test", "vendor/bin/pest", "composer.json:pestphp/pest"))
        if "phpunit/phpunit" in require:
            native.append(("test", "vendor/bin/phpunit", "composer.json:phpunit/phpunit"))
        if "laravel/pint" in require:
            native.append(("lint", "vendor/bin/pint --test", "composer.json:laravel/pint"))
        if {"phpstan/phpstan", "larastan/larastan", "nunomaduro/larastan"} & set(require):
            native.append(("typecheck", "vendor/bin/phpstan analyse", "composer.json:phpstan"))
        for script in ("test", "lint", "analyse", "analyze", "format"):
            if script in (data or {}).get("scripts", {}):
                cmd = {"analyse": "typecheck", "analyze": "typecheck"}.get(script, script)
                native.append((cmd, f"composer {script}", f"composer.json:scripts.{script}"))

        for cmd, run, source in native:
            wrapped = self.compose_exec(run)
            if wrapped and self.which("php") is None:
                self.candidate(cmd, wrapped, f"{source} (in container)", "medium", True,
                               "service guessed from the compose file; check it", priority=1)
            self.candidate(cmd, run, source, "high", priority=2)

    def node(self) -> None:
        data = self.read_json("package.json")
        if data is None:
            return
        r = self.r
        r.markers.append("package.json")
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        self.add(r.languages, "typescript" if (self.exists("tsconfig.json") or "typescript" in deps) else "javascript")
        pm_field = str(data.get("packageManager", ""))
        if pm_field.startswith(("pnpm", "yarn", "bun", "npm")):
            pm = pm_field.split("@")[0]
        elif self.exists("pnpm-lock.yaml"):
            pm = "pnpm"
        elif self.exists("yarn.lock"):
            pm = "yarn"
        elif self.exists("bun.lockb") or self.exists("bun.lock"):
            pm = "bun"
        else:
            pm = "npm"
        self.add(r.package_managers, pm)
        for dep, fw in (
            ("next", "next"), ("nuxt", "nuxt"), ("react", "react"), ("vue", "vue"), ("svelte", "svelte"),
            ("@sveltejs/kit", "sveltekit"), ("@angular/core", "angular"), ("vite", "vite"),
            ("express", "express"), ("@nestjs/core", "nestjs"), ("electron", "electron"),
            ("react-native", "react-native"),
        ):
            if dep in deps:
                self.add(r.frameworks, fw)
        scripts = data.get("scripts", {})
        mapping = {"test": "test", "lint": "lint", "build": "build", "typecheck": "typecheck",
                   "type-check": "typecheck", "check-types": "typecheck", "format": "format"}
        for script, cmd in mapping.items():
            body = str(scripts.get(script, ""))
            if not body or "no test specified" in body:
                continue
            if pm == "npm":
                run = "npm test" if script == "test" else f"npm run {script}"
            elif pm == "bun":
                run = f"bun run {script}"
            else:
                run = f"{pm} {script}" if script in ("test", "lint", "build") else f"{pm} run {script}"
            self.candidate(cmd, run, f"package.json:scripts.{script}", "high", priority=2)

    def go(self) -> None:
        text = _read_text(self.root / "go.mod")
        if text is None:
            return
        self.r.markers.append("go.mod")
        self.add(self.r.languages, "go")
        self.add(self.r.package_managers, "go")
        if any(self.exists(n) for n in (".golangci.yml", ".golangci.yaml", ".golangci.toml")):
            self.candidate("lint", "golangci-lint run", ".golangci config", "high", priority=2)
        self.candidate("test", "go test ./...", "go.mod", "high", priority=3)
        self.candidate("lint", "go vet ./...", "go.mod", "high", priority=3)
        self.candidate("build", "go build ./...", "go.mod", "high", priority=3)

    def python(self) -> None:
        names = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile")
        found = [n for n in names if self.exists(n)]
        if not found:
            return
        r = self.r
        r.markers.extend(found)
        self.add(r.languages, "python")
        pyproject = self.read_toml("pyproject.toml") or {}
        tool = pyproject.get("tool", {})
        text = " ".join((_read_text(self.root / n) or "") for n in found)
        if self.exists("uv.lock"):
            pm, prefix = "uv", "uv run "
        elif self.exists("poetry.lock") or "poetry" in tool:
            pm, prefix = "poetry", "poetry run "
        elif self.exists("Pipfile"):
            pm, prefix = "pipenv", "pipenv run "
        else:
            pm, prefix = "pip", ""
        self.add(r.package_managers, pm)
        if self.exists("manage.py"):
            self.add(r.frameworks, "django")
        for lib in ("fastapi", "flask"):
            if re.search(rf"\b{lib}\b", text, re.I):
                self.add(r.frameworks, lib)
        has_tests = any((self.root / d).is_dir() for d in ("tests", "test"))
        if "pytest" in tool or self.exists("pytest.ini") or re.search(r"\bpytest\b", text) or has_tests:
            run = f"{prefix}pytest" if prefix else "python3 -m pytest"
            self.candidate("test", run, "python project", "high", priority=3)
        if "ruff" in tool or self.exists("ruff.toml") or self.exists(".ruff.toml"):
            self.candidate("lint", f"{prefix}ruff check .", "ruff config", "high", priority=3)
        if "mypy" in tool or self.exists("mypy.ini"):
            self.candidate("typecheck", f"{prefix}mypy .", "mypy config", "high", priority=3)

    def others(self) -> None:
        r = self.r
        if self.exists("Cargo.toml"):
            r.markers.append("Cargo.toml")
            self.add(r.languages, "rust")
            self.add(r.package_managers, "cargo")
            self.candidate("test", "cargo test", "Cargo.toml", priority=3)
            self.candidate("lint", "cargo clippy -- -D warnings", "Cargo.toml", priority=3)
            self.candidate("build", "cargo build", "Cargo.toml", priority=3)
        pubspec = _read_text(self.root / "pubspec.yaml")
        if pubspec is not None:
            r.markers.append("pubspec.yaml")
            self.add(r.languages, "dart")
            tool = "flutter" if re.search(r"^\s*(flutter:|sdk:\s*flutter)", pubspec, re.M) else "dart"
            if tool == "flutter":
                self.add(r.frameworks, "flutter")
            self.add(r.package_managers, "pub")
            self.candidate("test", f"{tool} test", "pubspec.yaml", priority=3)
            self.candidate("lint", f"{tool} analyze", "pubspec.yaml", priority=3)
        if self.exists("pom.xml"):
            r.markers.append("pom.xml")
            self.add(r.languages, "java")
            self.add(r.package_managers, "maven")
            mvn = "./mvnw" if self.exists("mvnw") else "mvn"
            self.candidate("test", f"{mvn} -q test", "pom.xml", priority=3)
        gradle = next((n for n in ("build.gradle", "build.gradle.kts") if self.exists(n)), None)
        if gradle:
            r.markers.append(gradle)
            self.add(r.languages, "kotlin" if gradle.endswith(".kts") else "java")
            self.add(r.package_managers, "gradle")
            g = "./gradlew" if self.exists("gradlew") else "gradle"
            self.candidate("test", f"{g} test", gradle, priority=3)
            self.candidate("build", f"{g} build", gradle, priority=3)
        if self.exists("Gemfile"):
            r.markers.append("Gemfile")
            self.add(r.languages, "ruby")
            self.add(r.package_managers, "bundler")
            if self.exists("bin/rails"):
                self.add(r.frameworks, "rails")
            run = "bundle exec rspec" if (self.root / "spec").is_dir() else "bundle exec rake test"
            self.candidate("test", run, "Gemfile", priority=3)

    def finish(self) -> None:
        r = self.r
        for name, cands in r.candidates.items():
            for c in cands:
                if c.requires_services:
                    if self.which("docker") is None:
                        c.confidence = "low"
                        c.notes = (c.notes + "; " if c.notes else "") + "docker not found on this machine"
                    continue
                ok, binary = self.host_has(c.run)
                if not ok:
                    c.confidence = "low"
                    c.notes = (c.notes + "; " if c.notes else "") + f"`{binary}` not found on this machine"
            cands.sort(key=lambda c: (_CONFIDENCE_RANK[c.confidence], c.priority))
        r.markers = sorted(set(r.markers))
        if not r.languages:
            r.warnings.append("no known language markers at the top level (monorepo or unusual layout?)")
        if "test" not in r.candidates:
            r.warnings.append("no test command found; add one with: orch project command set")


def discover(path: str | Path, which: Which = shutil.which) -> DiscoveryReport:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError(f"not a directory: {root}")
    d = _Discovery(root, which)
    d.git()
    d.layout()
    d.makefile()
    d.php()
    d.node()
    d.go()
    d.python()
    d.others()
    d.finish()
    return d.r
