#!/usr/bin/env python3
"""Check dependencies, install user-local commands, and verify a private desktop."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parent
COMMANDS = {
    "computer-use": "computer_use.py",
    "virtual-browser": "virtual_browser.py",
}
REQUIRED = (
    "python3",
    "Xvfb",
    "xauth",
    "xdpyinfo",
    "systemd-run",
    "systemctl",
    "xdg-dbus-proxy",
    "magick",
    "xdotool",
    "xclip",
    "dbus-daemon",
    "busctl",
)


def command(*args, input=None):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "computer_use.py"), *args],
        input=input,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"computer-use {args[0]} failed")
    return result.stdout


def check_dependencies():
    if sys.platform != "linux":
        raise RuntimeError(
            "computer-use requires Linux with a running user systemd manager"
        )
    missing = [name for name in REQUIRED if not shutil.which(name)]
    if missing:
        raise RuntimeError(
            "Missing system commands: "
            + ", ".join(missing)
            + ". Install their distribution packages, then rerun setup. "
            "ImageMagick 7 supplies magick. Setup does not run sudo or install packages."
        )
    import computer_use

    computer_use.configure_session_environment()
    command("setup-accessibility")
    result = subprocess.run(
        ["systemctl", "--user", "show-environment"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError(
            "The user systemd manager is unavailable. Run setup inside a Linux login "
            "session with an active user manager and session D-Bus. "
            + result.stderr.strip()
        )


def install_commands(bin_dir):
    # Validate both names before creating either link; never replace user commands.
    for name, filename in COMMANDS.items():
        target = bin_dir / name
        source = SCRIPTS / filename
        if not source.is_file() or not os.access(source, os.X_OK):
            raise RuntimeError(f"Missing or non-executable bundled helper: {source}")
        if os.path.lexists(target) and not (
            target.is_symlink() and target.resolve() == source
        ):
            raise RuntimeError(
                f"Existing command at {target}; preserved it. Use --bin-dir with a "
                "different directory, or explicitly relocate the old command first."
            )
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, filename in COMMANDS.items():
        target = bin_dir / name
        if not target.is_symlink():
            target.symlink_to(SCRIPTS / filename)


def smoke_check():
    name = "setup-" + secrets.token_hex(8)
    session = Path.home() / ".local/state/computer-use" / name
    started = False
    try:
        command("start", name, "--size", "640x480")
        started = True
        with tempfile.TemporaryDirectory(prefix="computer-use-setup-") as directory:
            screenshot = Path(directory) / "desktop.png"
            command("screenshot", name, "--raw", "--output", str(screenshot))
            header = screenshot.read_bytes()[:24]
            if (
                header[:8] != b"\x89PNG\r\n\x1a\n"
                or len(header) != 24
                or struct.unpack(">II", header[16:24]) != (640, 480)
            ):
                raise RuntimeError("Desktop smoke check did not produce a 640x480 PNG")
            # Keep one X connection: an empty Xvfb resets when its last client exits.
            location = command(
                "input",
                name,
                "--",
                "mousemove",
                "10",
                "10",
                "getmouselocation",
                "--shell",
            )
            if "X=10\n" not in location or "Y=10\n" not in location:
                raise RuntimeError("Virtual pointer smoke check failed")
            marker = "computer-use setup " + secrets.token_hex(8)
            command("clipboard", name, "set", input=marker)
            if command("clipboard", name, "get").rstrip("\n") != marker:
                raise RuntimeError("Virtual clipboard smoke check failed")
    finally:
        # start normally cleans up its own failures; handle retained partial state too.
        if started or (session / "state.json").exists():
            command("stop", name)
    if session.exists():
        raise RuntimeError(f"Setup session cleanup incomplete: {session}")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local/bin",
        help="Command directory (default: ~/.local/bin); existing commands are preserved",
    )
    cli.add_argument(
        "--check",
        action="store_true",
        help="Check dependencies only; do not install links or start a desktop",
    )
    args = cli.parse_args()
    try:
        check_dependencies()
        if args.check:
            print(json.dumps({"dependencies_ready": True, "desktop_verified": False}))
            return 0
        bin_dir = args.bin_dir.expanduser().resolve()
        install_commands(bin_dir)
        smoke_check()
        print(
            json.dumps(
                {
                    "ready": True,
                    "bin_dir": str(bin_dir),
                    "commands": {name: str(bin_dir / name) for name in COMMANDS},
                    "optional_missing": [
                        name
                        for name in ("helium-browser", "uv")
                        if not shutil.which(name)
                    ],
                },
                indent=2,
            )
        )
        if any(shutil.which(name) != str(bin_dir / name) for name in COMMANDS):
            print(
                "For this shell, run: export PATH="
                + shlex.quote(str(bin_dir))
                + ':"$PATH"'
                + "\nOr invoke the absolute command paths above. No shell files were edited.",
                file=sys.stderr,
            )
        return 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
