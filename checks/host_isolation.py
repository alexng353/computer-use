"""Read-only probes of the host Hyprland desktop for isolation checks.

Records the focused window address, cursor position and a SHA-256 of the
clipboard. Clipboard contents are hashed in-process and never printed.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def host_environment():
    runtime = Path(f"/run/user/{os.getuid()}")
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime))
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        instances = sorted(
            (runtime / "hypr").glob("*/.socket.sock"),
            key=lambda path: path.stat().st_mtime,
        )
        if not instances:
            return None
        env["HYPRLAND_INSTANCE_SIGNATURE"] = instances[-1].parent.name
    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(runtime.glob("wayland-[0-9]"))
        if not sockets:
            return None
        env["WAYLAND_DISPLAY"] = sockets[0].name
    return env


def probe():
    """Return the host's focus, cursor and clipboard digest, or None without Hyprland."""
    env = host_environment()
    if env is None or not shutil.which("hyprctl") or not shutil.which("wl-paste"):
        return None

    def hyprctl(*args):
        return json.loads(
            subprocess.run(
                ["hyprctl", "-j", *args],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout
        )

    clipboard = subprocess.run(
        ["wl-paste", "--no-newline"], env=env, capture_output=True, timeout=5
    )
    return {
        "activewindow": hyprctl("activewindow").get("address"),
        "cursorpos": hyprctl("cursorpos"),
        "clipboard_sha256": hashlib.sha256(clipboard.stdout).hexdigest(),
        "clipboard_status": clipboard.returncode,
    }
