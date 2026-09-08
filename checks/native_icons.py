"""Real icon-model mode switching, native clicks, redraw rejection and cleanup."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from native_ocr import STATE, command, wait_until

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import computer_use


def main(directory):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-icons-" + secrets.token_hex(4)
    session_dir = STATE / name

    def capture(mode):
        command(
            "screenshot",
            name,
            "--targets",
            mode,
            "--output",
            str(directory / f"{mode}.png"),
        )
        return json.loads((session_dir / "ocr-snapshot.json").read_text())

    def at(data, center):
        x, y = center
        return min(
            (
                target
                for target in data["targets"]
                if target["bounds"][0] <= x < target["bounds"][2]
                and target["bounds"][1] <= y < target["bounds"][3]
            ),
            key=lambda target: (
                (target["bounds"][2] - target["bounds"][0])
                * (target["bounds"][3] - target["bounds"][1])
            ),
        )

    try:
        command("start", name, "--size", "1000x700", "--no-accessibility")
        command(
            "launch",
            name,
            "--id",
            "fixture",
            "--",
            sys.executable,
            str(Path(__file__).with_name("native_ocr.py")),
            "--fixture",
            str(directory),
        )
        wait_until(
            lambda: json.loads(command("windows", name).stdout), "fixture window"
        )
        text = capture("text")
        assert text["target_mode"] == "text"
        save = next(target for target in text["targets"] if target["text"] == "Save")
        icons = capture("icons")
        assert icons["target_mode"] == "icons" and icons["prefix"] != text["prefix"]
        assert all(
            target["kind"] == "visual" and target["text"] is None
            for target in icons["targets"]
        )
        command("query", name, "@" + save["ref"], ok=False)
        target = at(icons, save["center"])
        query = json.loads(command("query", name, "@" + target["ref"]).stdout)
        assert query["target_mode"] == "icons" and query["screen_size"] == [1000, 700]
        command("click", name, "@" + target["ref"])
        wait_until(
            lambda: (directory / "clicked.json").exists(),
            "visual target native callback",
        )
        assert json.loads((directory / "clicked.json").read_text()) == {
            "saved": 1,
            "pointer": target["center"],
        }
        command("query", name, "@" + target["ref"], ok=False)
        print(
            "PASS optional icon runtime, text-to-icons switch and real visual-target click",
            flush=True,
        )

        target = at(capture("icons"), save["center"])
        change = directory / "change-label"
        change.write_text("Changed")
        wait_until(lambda: not change.exists(), "background button redraw")
        rejected = command("click", name, "@" + target["ref"], ok=False)
        assert "pixels changed" in rejected.stderr
        assert not (session_dir / "ocr-snapshot.json").exists()
        icons = capture("icons")
        text = capture("text")
        assert text["target_mode"] == "text"
        command("query", name, "@" + icons["targets"][0]["ref"], ok=False)
        print(
            "PASS icon redraw rejection and explicit text fallback invalidates icon refs",
            flush=True,
        )

        state = computer_use.load(name)
        units = [
            state["prefix"] + suffix for suffix in ("-ocr.service", "-icons.service")
        ]
        assert all(state["units"].count(unit) == 1 for unit in units)
        subprocess.run(["systemctl", "--user", "stop", units[1]], check=True)
        capture("icons")
        assert computer_use.load(name)["units"].count(units[1]) == 1
        command("stop", name)
        assert not session_dir.exists() and all(
            not computer_use.active(unit) for unit in units
        )
        print(
            "PASS both resident workers are tracked once, restart and stop cleanly",
            flush=True,
        )
    finally:
        if session_dir.exists():
            command("stop", name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-icons-check-") as temporary:
        main(Path(temporary))
