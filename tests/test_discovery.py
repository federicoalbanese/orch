import json

from orch.discovery import discover, parse_compose_services, parse_makefile
from orch.gitutil import sanitize_remote

from conftest import all_tools, git, no_tools

LARAVEL = {
    "artisan": "#!/usr/bin/env php\n",
    "composer.json": json.dumps({"require": {"laravel/framework": "^11"},
                                 "require-dev": {"laravel/pint": "^1", "phpunit/phpunit": "^11"}}),
    "phpunit.xml": "<phpunit/>",
    "docker-compose.yml": "services:\n  nginx:\n    image: nginx\n  app:\n    build: .\n  mysql:\n    image: mysql\nvolumes:\n  data:\n",
    "package.json": json.dumps({"devDependencies": {"vite": "^5"}, "scripts": {"build": "vite build"}}),
    "tests/Feature/ExampleTest.php": "<?php\n",
}


def test_parse_makefile():
    text = ".PHONY: test\nVAR := 1\ntest: deps\n\tdocker compose exec app php artisan test\nlint fmt:\n\tpint\n%.o: %.c\n\tcc\n"
    targets = parse_makefile(text)
    assert targets["test"] == ["docker compose exec app php artisan test"]
    assert "lint" in targets and "fmt" in targets and ".PHONY" not in targets and "%.o" not in targets


def test_parse_compose_services():
    text = "version: '3'\nservices:\n  web:\n    image: x\n    ports:\n      - 80\n  db: # comment\n    image: y\nnetworks:\n  n: {}\n"
    assert parse_compose_services(text) == ["web", "db"]


def test_laravel_without_php_on_host_proposes_container_command(make_repo):
    root = make_repo("shop", LARAVEL)
    r = discover(root, which=lambda n: "/usr/bin/docker" if n == "docker" else None)
    assert "php" in r.languages and "laravel" in r.frameworks and "vite" in r.frameworks
    assert r.compose_services == ["nginx", "app", "mysql"]
    test = r.candidates["test"][0]
    assert test.run == "docker compose exec -T app php artisan test"
    assert test.requires_services and test.confidence == "medium"
    host = [c for c in r.candidates["test"] if c.run == "php artisan test"][0]
    assert host.confidence == "low" and "php" in host.notes
    assert r.candidates["lint"][0].run.endswith("vendor/bin/pint --test")
    assert r.proposed_commands()["test"].confirmed is False


def test_makefile_targets_win_and_detect_services(make_repo):
    files = dict(LARAVEL, Makefile="test:\n\tdocker compose exec -T app php artisan test\n")
    r = discover(make_repo("shop2", files), which=all_tools)
    assert r.candidates["test"][0].run == "make test"
    assert r.candidates["test"][0].requires_services


def test_node_go_python(make_repo):
    node = discover(make_repo("web", {
        "package.json": json.dumps({"dependencies": {"next": "14", "react": "18"}, "devDependencies": {"typescript": "5"},
                                    "scripts": {"test": "vitest", "lint": "eslint .", "type-check": "tsc"}}),
        "pnpm-lock.yaml": "",
    }), which=all_tools)
    assert node.package_managers == ["pnpm"] and "next" in node.frameworks and "typescript" in node.languages
    assert node.candidates["test"][0].run == "pnpm test"
    assert node.candidates["typecheck"][0].run == "pnpm run type-check"

    go = discover(make_repo("svc", {"go.mod": "module x\n\ngo 1.22\n", "main.go": "package main\n"}), which=all_tools)
    assert go.languages == ["go"] and go.candidates["test"][0].run == "go test ./..."

    py = discover(make_repo("py", {"pyproject.toml": "[tool.ruff]\n[tool.poetry]\n", "poetry.lock": "",
                                   "tests/test_x.py": ""}), which=all_tools)
    assert py.package_managers == ["poetry"]
    assert py.candidates["test"][0].run == "poetry run pytest"
    assert py.candidates["lint"][0].run == "poetry run ruff check ."


def test_npm_placeholder_test_script_is_ignored(make_repo):
    r = discover(make_repo("x", {"package.json": json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}})}),
                 which=all_tools)
    assert "test" not in r.candidates
    assert any("no test command" in w for w in r.warnings)


def test_git_info_and_credentials_are_stripped(make_repo):
    root = make_repo("g", {"go.mod": "module g\n"})
    git(root, "remote", "add", "origin", "https://user:s3cret@gitlab.example.com/team/g.git")
    (root / "new.txt").write_text("x")
    r = discover(root, which=no_tools)
    assert r.is_git and r.remote == "https://gitlab.example.com/team/g.git"
    assert r.default_branch == "main" and r.current_branch == "main" and r.dirty is True
    assert r.candidates["test"][0].confidence == "low"


def test_non_git_and_empty_dirs(make_repo):
    r = discover(make_repo("plain", {"notes.txt": "hi"}, git_init=False), which=no_tools)
    assert not r.is_git and any("not a git repository" in w for w in r.warnings)
    assert any("no known language" in w for w in r.warnings)


def test_sanitize_remote_keeps_ssh():
    assert sanitize_remote("ssh://git@host:2222/a/b.git") == "ssh://git@host:2222/a/b.git"
    assert sanitize_remote("git@host:a/b.git") == "git@host:a/b.git"
    assert sanitize_remote("https://oauth2:tok@host/a.git") == "https://host/a.git"
