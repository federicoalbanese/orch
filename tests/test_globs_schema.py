import pytest

from orch import globs
from orch.schema import PLAN_SCHEMA, REPORT_SCHEMA, REVIEW_SCHEMA, validate

from conftest import plan_dict


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("src/a.py", "src/*.py", True),
        ("src/x/a.py", "src/*.py", False),
        ("src/x/a.py", "src/**", True),
        ("src/x/a.py", "src/**/*.py", True),
        ("src/a.py", "src/**/*.py", True),
        ("config/.env.local", ".env.*", True),
        (".env", ".env", True),
        ("app/.env", ".env", True),
        ("database/migrations/2024_x.php", "database/migrations/", True),
        ("x/database/migrations/a.php", "database/migrations/", False),
        ("x/migrations/a.php", "migrations/", True),
        ("composer.json", "composer.json", True),
        ("packages/a/composer.json", "composer.json", True),
        ("./src/a.py", "src/a.py", True),
        ("srcx/a.py", "src/**", False),
        ("a/b/c.txt", "a/?/c.txt", True),
        ("a/bb/c.txt", "a/?/c.txt", False),
        ("v1.php", "v[0-9].php", True),
    ],
)
def test_glob_semantics(path, pattern, expected):
    assert globs.match(path, pattern) is expected


@pytest.mark.parametrize("pattern", ["/etc/passwd", "../x", "a/../b", "", "  ", "C:/x"])
def test_bad_patterns(pattern):
    assert globs.validate_pattern(pattern) is not None


def test_unbounded():
    assert globs.is_unbounded("**") and globs.is_unbounded("**/*")
    assert not globs.is_unbounded("src/**")


def test_plan_schema_accepts_valid_plan():
    assert validate(plan_dict("demo"), PLAN_SCHEMA) == []


def test_plan_schema_reports_every_error_with_a_path():
    bad = plan_dict("demo", tasks=[{"key": "1bad", "risk": "extreme", "extra": 1}], bogus=True)
    errors = validate(bad, PLAN_SCHEMA)
    joined = "\n".join(errors)
    assert "$.bogus: unknown field" in joined
    assert "$.tasks[0].key" in joined
    assert "$.tasks[0].risk" in joined
    assert "$.tasks[0].title: required" in joined
    assert "$.tasks[0].extra: unknown field" in joined


def test_schema_rejects_bool_as_integer_and_blank_strings():
    assert validate(True, {"type": "integer"})
    assert validate("   ", {"type": "string", "minLength": 1})


def test_report_and_review_schemas():
    assert validate({"status": "DONE", "files_changed": [], "summary": "s", "unresolved": []}, REPORT_SCHEMA) == []
    assert validate({"status": "MAYBE"}, REPORT_SCHEMA)
    review = {"decision": "NEEDS_CORRECTION", "summary": "x", "criteria": [{"id": "A1", "met": False}],
              "findings": [{"severity": "major", "category": "tests", "message": "no test", "line": 3}]}
    assert validate(review, REVIEW_SCHEMA) == []
    review["findings"][0]["line"] = 0
    assert validate(review, REVIEW_SCHEMA)
