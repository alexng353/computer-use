"""Exercise automatic screenshot output and fresh targets through the real CLI."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from native_ocr import CLI, STATE, command, wait_until


def main(directory):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-auto-" + secrets.token_hex(4)
    session = STATE / name
    images = directory / "screenshots with spaces"
    seen = set()

    def image_path(output, structured=False):
        value = (
            json.loads(output)["screenshot"] if structured else output.splitlines()[-1]
        )
        path = Path(value)
        assert path.is_absolute() and path.parent == images
        assert path.is_file() and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert path.stat().st_mode & 0o077 == 0, "Screenshots must be owner-only"
        assert path not in seen, "Every interaction must return a new image"
        seen.add(path)
        return path

    fixture = directory / "page.html"
    fixture.write_text(
        "<!doctype html><title>Automatic screenshot check</title>"
        '<body style="background:#123;font:24px sans-serif">'
        '<button style="margin:100px;padding:40px" onclick="'
        "this.textContent='Done';document.body.style.background='#357'"
        '">Next</button></body>'
    )
    try:
        created = command("start", name, "--screenshot-directory", str(images))
        image_path(created.stdout, structured=True)
        assert json.loads(command("status", name).stdout)[
            "screenshot_directory"
        ] == str(images)
        app = command("browser", name, "--no-cdp", "--url", fixture.as_uri())
        image_path(app.stdout, structured=True)
        wait_until(
            lambda: json.loads(command("windows", name).stdout), "browser window"
        )
        window = json.loads(command("windows", name).stdout)[0]["id"]
        image_path(command("input", name, "--", "windowfocus", str(window)).stdout)
        snapshot = json.loads((session / "ocr-snapshot.json").read_text())
        assert snapshot["target_mode"] == "accessibility"
        target = next(t for t in snapshot["targets"] if t["text"] == "Next")
        before_query = set(images.iterdir())
        command("query", name, "@" + target["ref"])
        assert set(images.iterdir()) == before_query, (
            "Read-only queries must retain references"
        )
        clicked = image_path(command("click", name, "@" + target["ref"]).stdout)
        fresh = json.loads((session / "ocr-snapshot.json").read_text())
        assert fresh["image"] == str(clicked)
        assert any(t["text"] == "Done" for t in fresh["targets"])
        command("query", name, "@" + target["ref"], ok=False)
        print(
            "PASS automatic images publish fresh, usable accessibility references",
            flush=True,
        )

        failed = command(
            "exec",
            name,
            "--",
            sys.executable,
            "-c",
            "print('payload');exit(7)",
            ok=False,
        )
        assert failed.returncode == 7 and failed.stdout.startswith("payload\n")
        image_path(failed.stdout)
        unterminated = command(
            "exec",
            name,
            "--",
            sys.executable,
            "-c",
            "import sys;sys.stdout.write('payload')",
        )
        image_path(unterminated.stdout)
        clipboard = subprocess.run(
            [sys.executable, str(CLI), "clipboard", name, "set"],
            input="private clipboard fixture",
            text=True,
            capture_output=True,
            check=True,
        )
        image_path(clipboard.stdout)
        assert command("clipboard", name, "get").stdout == "private clipboard fixture"
        state = json.loads(command("status", name).stdout)
        subprocess.run(
            [
                "systemctl",
                "--user",
                "stop",
                state["prefix"] + "-accessibility-registry.service",
            ],
            check=True,
        )
        fallback = command("input", name, "--", "mousemove", "50", "50")
        image_path(fallback.stdout)
        assert "returning a raw screenshot without references" in fallback.stderr
        assert not (session / "ocr-snapshot.json").exists()
        units = state["units"]
        command("stop", name)
        assert not session.exists() and all(path.is_file() for path in seen)
        assert all(
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit]
            ).returncode
            for unit in units
        )
        print(
            "PASS command exit status, clipboard output and retained screenshots survive cleanup",
            flush=True,
        )

        shortcut = subprocess.run(
            [
                sys.executable,
                str(CLI.with_name("virtual_browser.py")),
                "start",
                name,
                "--screenshot-directory",
                str(images),
                "--no-cdp",
                "--url",
                fixture.as_uri(),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        image_path(shortcut.stdout, structured=True)
        command("stop", name)
        print(
            "PASS virtual-browser forwards the session screenshot setting", flush=True
        )
        image_path(
            command(
                "start",
                name,
                "--no-accessibility",
                "--screenshot-directory",
                str(images),
            ).stdout,
            structured=True,
        )
        image_path(
            command(
                "launch",
                name,
                "--",
                sys.executable,
                "-c",
                "import tkinter;tkinter.Tk().mainloop()",
            ).stdout,
            structured=True,
        )
        assert not (session / "ocr-snapshot.json").exists()
        command("stop", name)
        print(
            "PASS native launches return raw images when accessibility is disabled",
            flush=True,
        )
    finally:
        if session.exists():
            command("stop", name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-auto-check-") as temporary:
        main(Path(temporary))
