import pytest

from orch.config import ACTIONS, NEVER_AUTOMATIC, build_config, default_config_text, load_config
from orch.errors import ConfigError


def test_defaults_are_valid_and_cover_every_action():
    cfg = build_config()
    auto = set(cfg.get("policy", "auto_actions"))
    approval = set(cfg.get("policy", "approval_actions"))
    assert auto | approval == set(ACTIONS) and not auto & approval
    assert cfg.requires_approval("git_push") and not cfg.requires_approval("edit_in_scope")


def test_default_config_text_round_trips(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(default_config_text())
    assert load_config(path).data == build_config().data


@pytest.mark.parametrize(
    "layer,needle",
    [
        ({"limits": {"max_review_cycles": "3"}}, "must be of type integer"),
        ({"limits": {"max_review_cycle": 3}}, "unknown key"),
        ({"nope": {}}, "unknown section"),
        ({"limits": {"max_review_cycles": 0}}, ">= 1"),
        ({"executor": {"default": "gpt"}}, "executor.default"),
        ({"policy": {"auto_actions": ["inspect", "frobnicate"]}}, "unknown action"),
        ({"policy": {"require_approval_for_risk": ["huge"]}}, "unknown levels"),
        ({"policy": {"sensitive_globs": ["/etc/*"]}}, "absolute path"),
    ],
)
def test_invalid_config_is_rejected(layer, needle):
    with pytest.raises(ConfigError) as e:
        build_config(layer)
    assert needle in str(e.value)


@pytest.mark.parametrize("action", sorted(NEVER_AUTOMATIC))
def test_never_automatic_floor(action):
    with pytest.raises(ConfigError, match="can never be automatic"):
        build_config({"policy": {"auto_actions": ["inspect", action]}})


def test_giving_one_action_list_derives_the_other():
    cfg = build_config({"policy": {"auto_actions": ["inspect", "git_commit"]}})
    assert not cfg.requires_approval("git_commit")
    assert cfg.requires_approval("edit_in_scope")
    assert "edit_in_scope" in cfg.get("policy", "approval_actions")


def test_project_overrides_merge_and_validate():
    cfg = build_config()
    merged = cfg.with_overrides({"limits": {"max_review_cycles": 5}}, "p")
    assert merged.get("limits", "max_review_cycles") == 5
    assert merged.get("limits", "auto_fix_attempts") == cfg.get("limits", "auto_fix_attempts")
    with pytest.raises(ConfigError):
        cfg.with_overrides({"policy": {"auto_actions": ["force_push"]}}, "p")


def test_unparseable_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[limits\n")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_config(path)
