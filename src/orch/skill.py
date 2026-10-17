"""Install the bundled Claude Code skill that teaches Claude the orch workflow."""

from __future__ import annotations

import filecmp
import shutil
from importlib import resources
from pathlib import Path

from orch.errors import OrchError

SKILL_NAME = "orchestrate"


def bundled_skill() -> Path:
    path = Path(str(resources.files("orch").joinpath(f"resources/claude/skills/{SKILL_NAME}/SKILL.md")))
    if not path.is_file():
        raise OrchError(f"bundled skill not found at {path} (broken installation?)")
    return path


def default_destination() -> Path:
    return Path.home() / ".claude" / "skills" / SKILL_NAME / "SKILL.md"


def install(dest: Path | None = None, force: bool = False) -> tuple[Path, str]:
    """Copy the skill into place. Returns (path, "installed" | "updated" | "unchanged")."""
    src = bundled_skill()
    target = (dest or default_destination()).expanduser()
    if target.suffix.lower() != ".md":  # a directory, existing or not
        target = target / "SKILL.md"
    if target.exists():
        if filecmp.cmp(src, target, shallow=False):
            return target, "unchanged"
        if not force:
            raise OrchError(f"{target} exists and differs from the bundled skill; rerun with --force to replace it")
        state = "updated"
    else:
        state = "installed"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    return target, state
