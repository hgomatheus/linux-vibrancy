"""Command line interface: `vibrance set 70`, `vibrance gui`, ..."""

from __future__ import annotations

import argparse
import sys

from . import autostart
from .backends import BACKENDS, Backend, BackendError, detect_backend, make_runner
from .colormath import DEFAULT_LEVEL, MAX_LEVEL, MIN_LEVEL
from .config import Config

GENERAL_TIPS = [
    "Night Light / blue-light filters also make colours look duller; turn them off while comparing.",
    "Check the monitor's own picture mode (sRGB/Standard modes on some monitors cap saturation).",
]


def level_arg(text: str) -> int:
    try:
        value = int(text.rstrip("%"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not MIN_LEVEL <= value <= MAX_LEVEL:
        raise argparse.ArgumentTypeError(f"level must be between {MIN_LEVEL} and {MAX_LEVEL}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibrance",
        description="NVIDIA-style digital vibrance for Linux. 50 is normal, 100 is maximum "
        "boost and 0 is greyscale, just like the NVIDIA Control Panel.",
    )
    parser.add_argument("--backend", choices=sorted(BACKENDS), help="skip auto-detection")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every command that is run")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("status", help="show the backend, displays and their levels (default)")

    p = sub.add_parser("set", help="set the vibrance level (0-100, 50 = normal)")
    p.add_argument("level", type=level_arg)
    p.add_argument("-o", "--output", action="append", help="only this display (repeatable)")

    p = sub.add_parser("reset", help="go back to normal colours (50)")
    p.add_argument("-o", "--output", action="append", help="only this display (repeatable)")

    sub.add_parser("restore", help="re-apply saved levels (used at login)")
    sub.add_parser("gui", help="open the slider window")
    sub.add_parser("doctor", help="look for settings that make colours look washed out")

    p = sub.add_parser("autostart", help="re-apply saved levels at every login")
    p.add_argument("state", choices=["on", "off", "status"])
    return parser


def select_outputs(backend: Backend, wanted: list[str] | None) -> list[str]:
    available = [o.name for o in backend.outputs()]
    if not available:
        raise BackendError("no active displays found")
    if not wanted or not backend.per_output:
        return available
    unknown = [name for name in wanted if name not in available]
    if unknown:
        raise BackendError(f"unknown display {', '.join(unknown)} (available: {', '.join(available)})")
    return wanted


def apply_level(backend: Backend, config: Config, outputs: list[str], level: int) -> None:
    try:
        for name in outputs:
            for note in backend.apply(name, level):
                print(f"note: {note}")
            config.set_level(backend.id, name, level)
            print(f"{name}: vibrance {level}%")
    finally:
        config.save()


def cmd_status(backend: Backend, config: Config, args) -> int:
    print(f"Backend: {backend.label}")
    for output in backend.outputs():
        level = backend.current_level(output.name)
        suffix = " (normal)" if level == DEFAULT_LEVEL else ""
        desc = f"  [{output.description}]" if output.description else ""
        print(f"  {output.name:<12} {level:>3}%{suffix}{desc}")
    print(f"Apply at login: {'on' if autostart.is_enabled() else 'off'}")
    return 0


def cmd_set(backend: Backend, config: Config, args) -> int:
    apply_level(backend, config, select_outputs(backend, args.output), args.level)
    return 0


def cmd_reset(backend: Backend, config: Config, args) -> int:
    apply_level(backend, config, select_outputs(backend, args.output), DEFAULT_LEVEL)
    return 0


def cmd_restore(backend: Backend, config: Config, args) -> int:
    try:
        for output in backend.outputs():
            backend.apply(output.name, config.level(backend.id, output.name))
    finally:
        config.save()
    return 0


def cmd_gui(backend: Backend, config: Config, args) -> int:
    from .gui import run_gui

    return run_gui(backend, config)


def cmd_doctor(backend: Backend, config: Config, args) -> int:
    print(f"Backend: {backend.label}\n")
    findings = backend.diagnose()
    if findings:
        print("Possible causes of washed-out colours:")
        for item in findings:
            print(f"  * {item}")
    else:
        print("No driver/compositor settings found that would wash out colours.")
    print("\nAlso worth checking:")
    for tip in GENERAL_TIPS:
        print(f"  * {tip}")
    return 0


def cmd_autostart(backend: Backend | None, args) -> int:
    if args.state == "on":
        print(f"Created {autostart.enable()}")
        if backend and backend.persistence_hint():
            print("\n" + backend.persistence_hint())
    elif args.state == "off":
        autostart.disable()
        print("Autostart removed")
    else:
        print("on" if autostart.is_enabled() else "off")
    return 0


COMMANDS = {
    "status": cmd_status,
    "set": cmd_set,
    "reset": cmd_reset,
    "restore": cmd_restore,
    "gui": cmd_gui,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config()
    run = make_runner(args.verbose)
    try:
        if args.command == "autostart":
            try:
                backend = detect_backend(config, run, args.backend)
            except BackendError:
                backend = None
            return cmd_autostart(backend, args)
        backend = detect_backend(config, run, args.backend)
        return COMMANDS[args.command or "status"](backend, config, args)
    except BackendError as exc:
        print(f"vibrance: {exc}", file=sys.stderr)
        if args.command == "gui":
            from .gui import show_error

            show_error(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
