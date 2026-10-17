import json

import pytest

from orch.cli import main
from orch.executors.agy import PROBE_PROMPT, VERIFIED_VERSION

from conftest import agy_result


@pytest.fixture
def doctor(tmp_path, monkeypatch, capsys, fake_agy_bin):
    monkeypatch.setenv("ORCH_AGY_BIN", str(fake_agy_bin))
    monkeypatch.setenv("ORCH_AGY_BRAIN_DIR", str(tmp_path / "brain"))
    monkeypatch.setenv("FAKE_AGY_ARGV", str(tmp_path / "argv.json"))
    fixture = tmp_path / "probe.jsonl"

    def run(*flags, lines=(), **env):
        fixture.write_text("\n".join(lines))
        monkeypatch.setenv("FAKE_AGY_FIXTURE", str(fixture))
        for key, value in env.items():
            monkeypatch.setenv(f"FAKE_AGY_{key.upper()}", value)
        code = main(["--home", str(tmp_path / "home"), "--json", "doctor", *flags])
        data = json.loads(capsys.readouterr().out)
        return code, data, {c["check"]: c for c in data["checks"]}

    run.argv_file = tmp_path / "argv.json"
    return run


def test_doctor_shows_the_agy_version_and_never_probes_unasked(doctor):
    code, data, checks = doctor()
    assert code == 0 and data["probe"] is None
    assert not doctor.argv_file.exists()  # agy only ran --version: nothing was sent anywhere
    version = checks["agy version"]
    assert not version["ok"] and not version["critical"]  # a warning, not a failure
    assert version["detail"].startswith("9.9.9-fake; orch was verified against " + VERIFIED_VERSION)
    assert "agy account (probe)" not in checks


def test_probe_success_is_one_read_only_turn(doctor):
    code, data, checks = doctor("--probe", lines=[agy_result(response="OK")])
    assert code == 0 and checks["agy account (probe)"]["ok"]
    assert "signed in and working" in checks["agy account (probe)"]["detail"]
    argv = json.loads(doctor.argv_file.read_text())
    assert argv[:2] == ["-p", PROBE_PROMPT]
    assert "--mode" not in argv and "--json-schema" not in argv  # read-only, no report contract
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"  # the model orch will actually use
    assert data["probe"]["status"] == "succeeded" and data["probe"]["usage"] == {"input_tokens": 10, "output_tokens": 2}


@pytest.mark.parametrize("stderr,classification,words", [
    ("Error: HTTP 400 User location is not supported for the API use.", "env_blocked", "network location"),
    ("HTTP 403 SUBSCRIPTION_REQUIRED (#3501)", "env_blocked", "no licence"),
    ("Eligibility check failed: region", "env_blocked", "not eligible"),
    ("Error: HTTP 429 Too Many Requests RESOURCE_EXHAUSTED", "transient", "try again later"),
    ("error: not logged in", "env_blocked", "not signed in"),
])
def test_probe_failures_are_classified(doctor, stderr, classification, words):
    code, data, checks = doctor("--probe", stderr=stderr, exit="1")
    probe = checks["agy account (probe)"]
    assert code == 1 and not data["ok"] and not probe["ok"] and probe["critical"]
    assert data["probe"]["classification"] == classification
    assert probe["detail"].startswith(classification + ":") and words in probe["detail"]


def test_probe_timeout_is_reported(doctor):
    code, data, checks = doctor("--probe", lines=[agy_result()], stderr="[agy] print timeout after 2m0s")
    assert code == 1 and data["probe"]["classification"] == "timeout"
    assert "try again" in checks["agy account (probe)"]["detail"]
