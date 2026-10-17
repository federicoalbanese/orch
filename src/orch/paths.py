"""Where orch keeps its configuration and data.

ORCH_HOME (or --home) puts everything under one directory, which tests and
throwaway runs use. Otherwise the XDG base directories apply.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP = "orch"


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "orch.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def shared_dir(self) -> Path:
        return self.data_dir / "shared"

    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def ensure(self) -> None:
        for d in (self.config_dir, self.data_dir, self.projects_dir, self.shared_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)


def resolve_paths(home: str | None = None, env: Mapping[str, str] = os.environ) -> Paths:
    home = home or env.get("ORCH_HOME")
    if home:
        base = Path(home).expanduser().resolve()
        return Paths(config_dir=base / "config", data_dir=base / "data")
    config_root = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    data_root = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return Paths(config_dir=config_root / APP, data_dir=data_root / APP)
