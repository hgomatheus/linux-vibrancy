"""XDG autostart entry that re-applies the saved levels at login."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from .config import autostart_file


def self_command() -> list[str]:
    installed = shutil.which("vibrance")
    if installed:
        return [installed]
    # Running from a git checkout: point Python at it explicitly.
    package_root = Path(__file__).resolve().parent.parent
    return ["env", f"PYTHONPATH={package_root}", sys.executable, "-m", "vibrance"]


def is_enabled() -> bool:
    return autostart_file().exists()


def enable() -> Path:
    path = autostart_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    command = " ".join(shlex.quote(part) for part in [*self_command(), "restore"])
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Digital Vibrance\n"
        "Comment=Re-apply the saved digital vibrance levels\n"
        f"Exec={command}\n"
        "Icon=preferences-desktop-color\n"
        "NoDisplay=true\n"
        "X-KDE-autostart-phase=2\n"
    )
    return path


def disable() -> None:
    autostart_file().unlink(missing_ok=True)
