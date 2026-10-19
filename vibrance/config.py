"""Saved vibrance levels and file locations."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .colormath import DEFAULT_LEVEL, clamp_level

APP = "linux-vibrancy"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def autostart_file() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP}.desktop"


class Config:
    """JSON file holding the level per output plus backend bookkeeping."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "config.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data.get("levels"), dict):
            self.data["levels"] = {}

    @staticmethod
    def _key(backend_id: str, output: str) -> str:
        # Output names differ between X11 and Wayland (HDMI-0 vs HDMI-A-1),
        # so levels are remembered per backend.
        return f"{backend_id}:{output}"

    def level(self, backend_id: str, output: str) -> int:
        return clamp_level(self.data["levels"].get(self._key(backend_id, output), DEFAULT_LEVEL))

    def set_level(self, backend_id: str, output: str, level: int) -> None:
        self.data["levels"][self._key(backend_id, output)] = clamp_level(level)

    def section(self, name: str) -> dict:
        return self.data.setdefault(name, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)
