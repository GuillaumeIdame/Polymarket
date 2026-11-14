from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "settings.yaml"


@dataclass
class Settings:
    raw: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def data_dir(self) -> Path:
        p = Path(os.environ.get("PMKT_DATA_DIR", self.raw["data_dir"]))
        if not p.is_absolute():
            p = REPO_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def endpoint(self, name: str) -> str:
        env = os.environ.get(f"PMKT_{name.upper()}_URL")
        return env or self.raw["endpoints"][name]


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("PMKT_CONFIG", DEFAULT_CONFIG))
    with open(path) as f:
        return Settings(yaml.safe_load(f))
