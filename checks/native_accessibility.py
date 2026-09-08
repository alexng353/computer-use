"""Real browser AT-SPI targets, native clicks, redraw/focus rejection and cleanup."""

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

HTML = """<!doctype html><title>Accessibility verification</title>
<style>body{background:#202428;color:white;font:24px sans-serif;margin:50px}
button{font-size:30px;width:110px;height:70px;margin:20px}
#photo{width:320px;height:140px;background:#247;padding:20px}</style>
<h1>Accessibility verification</h1>
<button aria-label="Close" onclick="document.querySelector('#photo').textContent='Closed'">&times;</button>
<button aria-label="Next image" onclick="document.querySelector('#photo').textContent='Photo 2'">→</button>
<button disabled>Disabled</button><button style="display:none">Hidden</button>
<button aria-label="Change name" onclick="document.querySelector('[aria-label=Close]').setAttribute('aria-label','Dismiss')">Name</button>
<div id="photo">Photo 1</div>
<div style="position:relative;width:160px;height:100px">
<button>Covered</button>
<div style="position:absolute;inset:0;background:#567;z-index:10" aria-hidden="true"></div>
</div>
"""


def main(directory):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-a11y-" + secrets.token_hex(4)
    session_dir = STATE / name
    fixture = directory / "fixture.html"
    fixture.write_text(HTML)

    def capture(mode="accessibility"):
        command(
            "screenshot",
            name,
            "--targets",
            mode,
            "--output",
            str(directory / f"{mode}.png"),
        )
        return json.loads((session_dir / "ocr-snapshot.json").read_text())

    def button(snapshot, name):
        return next(
            t
            for t in snapshot["targets"]
            if t.get("role") == "button" and t["text"] == name and t["bounds"][1] > 100
        )

    try:
        command("start", name, "--size", "1000x700", "--accessibility")
        app = json.loads(
            command("browser", name, "--no-cdp", "--url", fixture.as_uri()).stdout
        )
        assert app["cdp_port"] is None
        assert not (Path(app["profile"]) / "DevToolsActivePort").exists()
        wait_until(
            lambda: json.loads(command("windows", name).stdout), "browser window"
        )
        window = json.loads(command("windows", name).stdout)[0]["id"]
        command("input", name, "--", "windowfocus", str(window))
        snapshot = capture()
        assert snapshot["target_mode"] == "accessibility"
        assert button(snapshot, "Close") and button(snapshot, "Next image")
        assert not any(
            t["text"] in ("Disabled", "Hidden", "Covered") for t in snapshot["targets"]
        )
        next_target = button(snapshot, "Next image")
        command("click", name, "@" + next_target["ref"])
        assert not (session_dir / "ocr-snapshot.json").exists()
        text = capture("text")
        assert any("Photo 2" in t["text"] for t in text["targets"])
        command("query", name, "@" + next_target["ref"], ok=False)
        print(
            "PASS native accessibility next-image click and mode invalidation",
            flush=True,
        )

        snapshot = capture()
        close = button(snapshot, "Close")
        change = button(snapshot, "Change name")
        # Background native input bypasses CLI invalidation to test fresh semantics.
        env = dict(os.environ, **computer_use.environment(computer_use.load(name)))
        subprocess.run(
            ["xdotool", "mousemove", *map(str, change["center"]), "click", "1"],
            env=env,
            check=True,
        )
        rejected = command("click", name, "@" + close["ref"], ok=False)
        assert "changed" in rejected.stderr, rejected.stderr
        assert not (session_dir / "ocr-snapshot.json").exists()
        snapshot = capture()
        dismiss = button(snapshot, "Dismiss")
        command("click", name, "@" + dismiss["ref"])
        assert any("Closed" in t["text"] for t in capture("text")["targets"])
        print(
            "PASS unchanged pixels with changed accessible name reject stale clicks",
            flush=True,
        )

        state = computer_use.load(name)
        cover = computer_use.launch(
            state, "cover", [sys.executable, __file__, "--cover"]
        )
        wait_until(
            lambda: len(json.loads(command("windows", name).stdout)) == 2,
            "covering native window",
        )
        subprocess.run(["xdotool", "windowfocus", str(window)], env=env, check=True)
        assert capture()["targets"] == [], (
            "Covered native window must not publish underlying targets"
        )
        subprocess.run(["systemctl", "--user", "stop", cover["unit"]], check=True)
        print("PASS another native window occludes focused-window targets", flush=True)

        snapshot = capture()
        close = button(snapshot, "Dismiss")
        subprocess.run(["xdotool", "windowfocus", "1"], env=env, check=True)
        command("click", name, "@" + close["ref"], ok=False)
        command("input", name, "--", "windowfocus", str(window))
        capture()
        state = computer_use.load(name)
        worker = state["prefix"] + "-accessibility.service"
        subprocess.run(["systemctl", "--user", "stop", worker], check=True)
        capture()
        assert computer_use.load(name)["units"].count(worker) == 1
        units = computer_use.load(name)["units"]
        command("stop", name)
        assert not session_dir.exists()
        assert not any(computer_use.active(unit) for unit in units)
        print(
            "PASS focus rejection, worker restart and private bus cleanup", flush=True
        )
    finally:
        if session_dir.exists():
            command("stop", name)


if __name__ == "__main__":
    if "--cover" in sys.argv:
        import tkinter as tk

        root = tk.Tk()
        root.title("Cover")
        root.overrideredirect(True)
        root.geometry("1000x700+0+0")
        root.configure(bg="#803030")
        root.mainloop()
        raise SystemExit(0)
    with tempfile.TemporaryDirectory(prefix="computer-use-a11y-check-") as temporary:
        main(Path(temporary))
