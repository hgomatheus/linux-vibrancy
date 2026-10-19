"""Backends that actually change the colours on screen.

Which one is used depends on the session:

* KDE Plasma (Wayland)  -> KWin, through a generated ICC colour profile
* Hyprland              -> a GLSL screen shader
* X11 + NVIDIA driver   -> nvidia-settings DigitalVibrance (same as Windows)
* X11 + AMD / Intel     -> the RandR "CTM" colour matrix property
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import colormath
from .colormath import DEFAULT_LEVEL
from .config import Config, data_dir


class BackendError(RuntimeError):
    pass


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str = ""


Runner = Callable[[list[str]], Result]


def make_runner(verbose: bool = False) -> Runner:
    def run(cmd: list[str]) -> Result:
        if verbose:
            print("+ " + " ".join(cmd), file=sys.stderr)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except FileNotFoundError:
            raise BackendError(f"{cmd[0]} is not installed") from None
        except subprocess.TimeoutExpired:
            raise BackendError(f"{cmd[0]} did not respond") from None
        if verbose and (proc.stdout.strip() or proc.stderr.strip()):
            print((proc.stdout + proc.stderr).rstrip(), file=sys.stderr)
        return Result(proc.returncode, proc.stdout, proc.stderr)

    return run


@dataclass
class Output:
    name: str
    description: str = ""


class Backend:
    id = ""
    label = ""
    # False when one setting covers every monitor (Hyprland's screen shader).
    per_output = True

    def __init__(self, config: Config, run: Runner):
        self.config = config
        self.run = run

    def outputs(self) -> list[Output]:
        raise NotImplementedError

    def apply(self, output: str, level: int) -> list[str]:
        """Set the level for one output and return notes worth showing the user."""
        raise NotImplementedError

    def current_level(self, output: str) -> int:
        """Level actually active right now; falls back to the saved one."""
        return self.config.level(self.id, output)

    def diagnose(self) -> list[str]:
        return []

    def persistence_hint(self) -> str | None:
        return None


# --- KDE Plasma / KWin --------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CONNECTOR_TYPES = {
    "VGA", "DVI", "DVII", "DVIA", "DVID", "HDMI", "Panel", "TV", "TVComposite",
    "TVSVideo", "TVComponent", "TVSCART", "TVC4", "DisplayPort", "Unknown",
}


@dataclass
class KScreenOutput:
    name: str
    connected: bool = False
    enabled: bool = True
    connector: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def icc_profile(self) -> str:
        value = self.fields.get("ICC profile", "")
        return "" if value in ("", "none", "incapable") else value

    @property
    def profile_source(self) -> str | None:
        # Missing on Plasma 6.0, where setting a profile path is enough.
        return self.fields.get("Color profile source")

    @property
    def rgb_range(self) -> str:
        return self.fields.get("RgbRange", "unknown")

    @property
    def hdr(self) -> bool:
        return self.fields.get("HDR") == "enabled"

    @property
    def wcg(self) -> bool:
        return self.fields.get("Wide Color Gamut") == "enabled"


def parse_kscreen_outputs(text: str) -> dict[str, KScreenOutput]:
    outputs: dict[str, KScreenOutput] = {}
    current: KScreenOutput | None = None
    for raw in ANSI_RE.sub("", text).splitlines():
        line = raw.strip()
        header = re.match(r"Output:\s+\d+\s+(\S+)", line)
        if header:
            current = outputs[header.group(1)] = KScreenOutput(header.group(1))
            continue
        if current is None or not line:
            continue
        if line in ("enabled", "disabled"):
            current.enabled = line == "enabled"
        elif line in ("connected", "disconnected"):
            current.connected = line == "connected"
        elif line in CONNECTOR_TYPES:
            current.connector = line
        elif ":" in line:
            key, _, value = line.partition(":")
            current.fields.setdefault(key.strip(), value.strip())
    return outputs


class KWinBackend(Backend):
    id = "kwin"
    label = "KDE Plasma (KWin colour profile)"
    PROFILE_RE = re.compile(r"vibrance-(\d+)\.icc$")

    @staticmethod
    def icc_dir() -> Path:
        return data_dir() / "icc"

    def _query(self) -> dict[str, KScreenOutput]:
        result = self.run(["kscreen-doctor", "-o"])
        if result.returncode != 0:
            raise BackendError("kscreen-doctor -o failed: " + (result.stderr or result.stdout).strip())
        return parse_kscreen_outputs(result.stdout)

    def _info(self, output: str) -> KScreenOutput:
        info = self._query().get(output)
        if info is None:
            raise BackendError(f"KDE doesn't know an output called {output}")
        return info

    def _set(self, args: list[str]) -> None:
        result = self.run(["kscreen-doctor", *args])
        text = (result.stdout + result.stderr).strip()
        if result.returncode != 0 or "applying config failed" in text or "Wrong input" in text:
            raise BackendError("kscreen-doctor refused the change: " + text)

    def _our_level(self, info: KScreenOutput) -> int | None:
        """Level of our profile if it is the one KWin is using, else None."""
        path = info.icc_profile
        match = self.PROFILE_RE.search(path)
        if not match or Path(path).parent != self.icc_dir():
            return None
        if info.profile_source not in (None, "ICC"):
            return None
        return int(match.group(1))

    def profile_path(self, level: int) -> Path:
        path = self.icc_dir() / f"vibrance-{level}.icc"
        data = colormath.build_icc_profile(level)
        if not path.exists() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return path

    def outputs(self) -> list[Output]:
        return [
            Output(o.name, o.connector)
            for o in self._query().values()
            if o.connected and o.enabled
        ]

    def current_level(self, output: str) -> int:
        level = self._our_level(self._info(output))
        return DEFAULT_LEVEL if level is None else level

    def apply(self, output: str, level: int) -> list[str]:
        info = self._info(output)
        notes: list[str] = []
        originals = self.config.section("kwin_original")
        active = self._our_level(info)
        has_source = info.profile_source is not None

        if level == DEFAULT_LEVEL:
            if active is not None:
                # Put back whatever colour profile the user had before.
                orig = originals.get(output, {"source": "sRGB", "icc": ""})
                args = [f"output.{output}.iccprofile.{orig['icc']}"]
                if has_source:
                    args.append(f"output.{output}.colorProfileSource.{orig['source']}")
                self._set(args)
            originals.pop(output, None)
            return notes

        floor = colormath.min_icc_level()
        if level < floor:
            notes.append(f"KWin can't desaturate below {floor}%, using {floor}%")
            level = floor

        if active is None:
            ours = Path(info.icc_profile).parent == self.icc_dir() if info.icc_profile else False
            originals[output] = {
                "source": info.profile_source or "sRGB",
                "icc": originals.get(output, {}).get("icc", "") if ours else info.icc_profile,
            }
            if info.profile_source == "ICC" and info.icc_profile and not ours:
                notes.append(
                    f"{output} was using the ICC profile {info.icc_profile}; it is replaced "
                    "while vibrance is active and comes back when you return to 50%"
                )

        args = [f"output.{output}.iccprofile.{self.profile_path(level)}"]
        if has_source:
            args.append(f"output.{output}.colorProfileSource.ICC")
        self._set(args)

        if info.hdr:
            notes.append(
                f"HDR is on for {output}, and KWin only uses this profile in SDR mode. In HDR, "
                "use the 'SDR color intensity' slider in System Settings > Display instead"
            )
        elif info.wcg:
            notes.append(
                f"Wide Color Gamut is on for {output}, which makes KWin ignore colour profiles. "
                "Turn it off in System Settings > Display for vibrance to take effect"
            )
        return notes

    def diagnose(self) -> list[str]:
        found = []
        for o in self._query().values():
            if not (o.connected and o.enabled):
                continue
            if o.rgb_range == "Limited":
                found.append(
                    f"{o.name}: RGB range is set to Limited. That makes blacks grey and every colour "
                    f"look washed out on a normal PC monitor. Fix: kscreen-doctor output.{o.name}.rgbrange.full"
                )
            elif o.rgb_range == "Automatic" and o.connector == "HDMI":
                found.append(
                    f"{o.name}: RGB range is Automatic on an HDMI connection, and many monitors end up in "
                    "Limited range that way (grey blacks, flat colours). If that's what you see, force Full: "
                    f"kscreen-doctor output.{o.name}.rgbrange.full"
                )
            if o.profile_source == "EDID" and not o.hdr:
                found.append(
                    f"{o.name}: colour profile is 'Built-in' (EDID). KWin then shows sRGB colours accurately "
                    "inside your monitor's wider gamut, which looks less saturated than Windows does by "
                    f"default. For the Windows look: kscreen-doctor output.{o.name}.colorProfileSource.sRGB"
                )
            if o.wcg and not o.hdr:
                found.append(
                    f"{o.name}: Wide Color Gamut is enabled. This also maps colours accurately (less "
                    "saturated than Windows) and blocks the vibrance profile. Consider turning it off"
                )
            if o.hdr:
                found.append(
                    f"{o.name}: HDR is enabled. Desktop colours are controlled by 'SDR color intensity' "
                    f"in System Settings > Display (kscreen-doctor output.{o.name}.sdrGamut.0-100)"
                )
        return found


# --- Hyprland -----------------------------------------------------------------

class HyprlandBackend(Backend):
    id = "hyprland"
    label = "Hyprland (screen shader)"
    per_output = False
    ALL = "all"

    def __init__(self, config: Config, run: Runner):
        super().__init__(config, run)
        self._method: str | None = None

    @staticmethod
    def shader_path() -> Path:
        return data_dir() / "hyprland-vibrance.frag"

    def outputs(self) -> list[Output]:
        return [Output(self.ALL, "all monitors")]

    def _set_option(self, value: str) -> None:
        # Hyprland <= 0.5x speaks hyprlang ("keyword"), newer releases are configured in Lua.
        if self._method in (None, "keyword"):
            result = self.run(["hyprctl", "keyword", "decoration:screen_shader", value or "[[EMPTY]]"])
            if result.returncode == 0 and result.stdout.strip() == "ok":
                self._method = "keyword"
                return
        lua = "hl.config({ decoration = { screen_shader = %s } })" % json.dumps(value, ensure_ascii=False)
        result = self.run(["hyprctl", "eval", lua])
        if result.returncode == 0 and result.stdout.strip() == "ok":
            self._method = "eval"
            return
        raise BackendError("hyprctl couldn't set decoration:screen_shader: " + (result.stdout + result.stderr).strip())

    def apply(self, output: str, level: int) -> list[str]:
        path = self.shader_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(colormath.hyprland_shader(level))
        # Clearing first makes Hyprland re-read the file even if the path is unchanged.
        self._set_option("")
        if level != DEFAULT_LEVEL:
            self._set_option(str(path))
        return []

    def persistence_hint(self) -> str:
        path = self.shader_path()
        return (
            "Hyprland forgets runtime changes on restart or `hyprctl reload`. Point your config at the "
            "shader file this tool keeps up to date:\n"
            f"  hyprland.conf: decoration:screen_shader = {path}\n"
            f'  hyprland.lua:  hl.config({{ decoration = {{ screen_shader = "{path}" }} }})\n'
            "(This replaces any other screen shader you use, such as a blue-light filter.)"
        )


# --- X11: NVIDIA ----------------------------------------------------------------

class NvidiaX11Backend(Backend):
    id = "nvidia"
    label = "NVIDIA on X11 (nvidia-settings Digital Vibrance)"
    DPY_RE = re.compile(r"\[dpy:\d+\]\s*\(([^)]+)\)(?:\s*\(([^)]*)\))?")

    def outputs(self) -> list[Output]:
        result = self.run(["nvidia-settings", "-q", "dpys"])
        if result.returncode != 0:
            return []
        found = []
        for match in self.DPY_RE.finditer(result.stdout):
            name, status = match.group(1), match.group(2)
            if status is None or "enabled" in status.split(", "):
                found.append(Output(name, "NVIDIA"))
        return found

    def _query(self, output: str, attribute: str) -> int | None:
        result = self.run(["nvidia-settings", "-t", "-q", f"[DPY:{output}]/{attribute}"])
        try:
            return int(result.stdout.strip().splitlines()[0])
        except (IndexError, ValueError):
            return None

    def current_level(self, output: str) -> int:
        value = self._query(output, "DigitalVibrance")
        return super().current_level(output) if value is None else colormath.nvidia_to_level(value)

    def apply(self, output: str, level: int) -> list[str]:
        value = colormath.level_to_nvidia(level)
        result = self.run(["nvidia-settings", "-a", f"[DPY:{output}]/DigitalVibrance={value}"])
        text = (result.stdout + result.stderr).strip()
        if result.returncode != 0 or "ERROR" in text:
            raise BackendError("nvidia-settings failed: " + text)
        return []

    def diagnose(self) -> list[str]:
        found = []
        for o in self.outputs():
            if self._query(o.name, "ColorRange") == 1:
                found.append(
                    f"{o.name}: output colour range is Limited (grey blacks, washed-out colours). "
                    f"Fix: nvidia-settings -a '[DPY:{o.name}]/ColorRange=0'"
                )
            if self._query(o.name, "ColorSpace") in (1, 2):
                found.append(
                    f"{o.name}: output colour format is YCbCr rather than RGB, which often looks flatter. "
                    f"Fix: nvidia-settings -a '[DPY:{o.name}]/ColorSpace=0'"
                )
        return found


# --- X11: AMD / Intel -----------------------------------------------------------

class XrandrCtmBackend(Backend):
    id = "xrandr"
    label = "X11 colour matrix (xrandr CTM)"

    def outputs(self) -> list[Output]:
        result = self.run(["xrandr", "--prop"])
        if result.returncode != 0:
            return []
        found: list[Output] = []
        current: str | None = None
        for line in result.stdout.splitlines():
            head = re.match(r"(\S+) connected (?:primary )?\d+x\d+\+", line)
            if head:
                current = head.group(1)
            elif not line.startswith(("\t", " ")):
                current = None
            elif current and line.strip().startswith("CTM:"):
                found.append(Output(current))
                current = None
        return found

    def apply(self, output: str, level: int) -> list[str]:
        matrix = colormath.saturation_matrix(colormath.level_to_saturation(level))
        values = ",".join(str(v) for v in colormath.ctm_property_values(matrix))
        result = self.run(["xrandr", "--output", output, "--set", "CTM", values])
        if result.returncode != 0:
            raise BackendError("xrandr failed: " + (result.stdout + result.stderr).strip())
        return []


BACKENDS: dict[str, type[Backend]] = {
    cls.id: cls for cls in (KWinBackend, HyprlandBackend, NvidiaX11Backend, XrandrCtmBackend)
}


def detect_backend(config: Config, run: Runner, forced: str | None = None, env=os.environ) -> Backend:
    if forced:
        return BACKENDS[forced](config, run)

    session = env.get("XDG_SESSION_TYPE", "").lower()
    if not session:
        session = "wayland" if env.get("WAYLAND_DISPLAY") else "x11" if env.get("DISPLAY") else ""
    desktop = env.get("XDG_CURRENT_DESKTOP", "").lower()

    if env.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        return HyprlandBackend(config, run)

    if session == "wayland":
        if ("kde" in desktop or env.get("KDE_FULL_SESSION")) and shutil.which("kscreen-doctor"):
            return KWinBackend(config, run)
        name = env.get("XDG_CURRENT_DESKTOP") or "this compositor"
        raise BackendError(
            f"{name} on Wayland has no interface other programs can use to change colour "
            "saturation. Supported: KDE Plasma, Hyprland, or any X11 session"
        )

    if session == "x11":
        if shutil.which("nvidia-settings"):
            backend = NvidiaX11Backend(config, run)
            if backend.outputs():
                return backend
        if shutil.which("xrandr"):
            backend = XrandrCtmBackend(config, run)
            if backend.outputs():
                return backend
        raise BackendError(
            "no supported X11 colour control found: install nvidia-settings (NVIDIA) or "
            "xorg-xrandr (AMD/Intel, needs a driver that exposes the CTM property)"
        )

    raise BackendError("no graphical session found (XDG_SESSION_TYPE, WAYLAND_DISPLAY and DISPLAY are unset)")
