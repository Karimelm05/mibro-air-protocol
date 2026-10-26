"""Paths and settings. Config lives in ~/.config/mibro/config.toml, data in
~/.local/share/mibro/ (XDG dirs respected). $MIBRO_ADDR overrides the address."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "mibro"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "mibro"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class Config:
    address: str = ""
    adapter: str = "hci0"
    use_path: bool = True          # attach via BlueZ object path (README §IV.4)
    db_path: Path = DATA_DIR / "mibro.db"
    port: int = 8765
    backfill_days: int = 1         # first sync: yesterday 00:00 -> now

    @property
    def lock_path(self) -> Path:
        return self.db_path.with_suffix(".lock")


def load() -> Config:
    cfg = Config()
    if CONFIG_FILE.exists():
        data = tomllib.loads(CONFIG_FILE.read_text())
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, Path(v).expanduser() if k == "db_path" else v)
    if os.environ.get("MIBRO_ADDR"):
        cfg.address = os.environ["MIBRO_ADDR"]
    if os.environ.get("MIBRO_DB"):
        cfg.db_path = Path(os.environ["MIBRO_DB"]).expanduser()
    return cfg


def save_address(address: str, adapter: str = "hci0") -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    if CONFIG_FILE.exists():
        lines = [ln for ln in CONFIG_FILE.read_text().splitlines()
                 if not ln.strip().startswith(("address", "adapter"))]
    lines = [f'address = "{address.upper()}"', f'adapter = "{adapter}"'] + lines
    CONFIG_FILE.write_text("\n".join(lines) + "\n")
    return CONFIG_FILE
