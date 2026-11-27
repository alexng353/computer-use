"""Exercise the persistent session REPL against real dialogs on Xvfb.

One REPL call opens zenity, types three lines, presses OK and asserts the
dialog's output. Also checks the observation helpers, state persistence,
error and timeout recovery, stdin cells, the cdp global and its reference
invalidation, spawn logs, the caller's working directory, subprocess output,
stop with a live child, and that the host focus, cursor and clipboard stay
unchanged.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from host_isolation import probe
from native_ocr import CLI, STATE, command

LINES = ["first line", "second line", "third line"]

# GTK4 zenity reports every AT-SPI extent at 0,0 without a window manager, so
# the OK button is targeted by its OCR text.
ZENITY_CELL = """
p = spawn("zenity", "--text-info", "--editable", "--title=Repl notes",
          "--width=600", "--height=400", capture=True)
focus(wait_window("Repl notes"))
wait_stable()
type("first line\\nsecond line\\nthird line")
shot = screenshot(targets="text")
click(find(shot, text="OK"))
out, err = p.communicate(timeout=10)
assert p.returncode == 0, (p.returncode, err)
assert out.splitlines() == ["first line", "second line", "third line"], out
print("zenity:", out.splitlines())
display(shot)
"""

OBSERVE_CELL = """
info = spawn("zenity", "--info", "--title=Observe", "--text=Status ready")
focus(wait_window("Observe"))
wait_stable()
tree = a11y()
assert "button 'OK'" in tree, tree
texts = [box["text"] for box in ocr()]
assert any("Status ready" in text for text in texts), texts
detail = zoom((0, 0, 200, 120))
display(detail)
key("Return")
info.wait(timeout=10)
print("observed", len(tree.splitlines()) > 3, info.returncode)
"""

FAKE_AGENT_BROWSER = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/agent-browser.log"
echo "fake $5"
"""


def images(stdout):
    return [
        line.removeprefix("[image] ")
        for line in stdout.splitlines()
        if line.startswith("[image] ")
    ]


def repl(name, code, cwd=None, stdin=None, ok=True):
    result = subprocess.run(
        [sys.executable, str(CLI), "repl", name, *(["--code", code] if code else [])],
        cwd=cwd,
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (result.returncode == 0) == ok, result
    return result


def main(directory):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-repl-" + secrets.token_hex(4)
    session_dir = STATE / name
    host_before = probe()
    if host_before is None:
        print("SKIP host isolation probes: no Hyprland session found", flush=True)
    try:
        command("start", name, "--size", "1000x700")

        result = repl(name, ZENITY_CELL)
        assert f"zenity: {LINES}" in result.stdout, result
        shots = images(result.stdout)
        assert len(shots) == 1 and Path(shots[0]).is_file(), result.stdout
        print("PASS one repl call drives zenity end to end", flush=True)

        observed = repl(name, OBSERVE_CELL)
        assert "observed True 0" in observed.stdout, observed
        [crop] = images(observed.stdout)
        with Image.open(crop) as image:
            # 200x120 is below the 800 px target, so zoom upscales it 4x.
            assert image.size == (800, 480), image.size
            colors = image.convert("RGB").getcolors(maxcolors=1 << 20)
            assert len(colors) > 10, "blank crop"
        print("PASS a11y(), ocr() and zoom() read a live dialog", flush=True)

        repl(name, "counter = 41")
        assert repl(name, "counter + 1").stdout.strip() == "42"
        failed = repl(name, "1 / 0", ok=False)
        assert "ZeroDivisionError" in failed.stderr, failed.stderr
        assert repl(name, "print(counter)").stdout == "41\n"
        timed_out = command(
            "repl",
            name,
            "--timeout",
            "1",
            "--code",
            "import time\ntime.sleep(30)",
            ok=False,
        )
        assert "interrupted" in timed_out.stderr.lower(), timed_out.stderr
        cell = directory / "cell.py"
        cell.write_text("print(counter * 2)\n")
        with cell.open() as stdin:
            assert repl(name, None, stdin=stdin).stdout == "82\n"
        print("PASS state persists across calls, errors and timeouts", flush=True)

        # F3: cdp is bound, and follows a browser recorded between cells.
        assert repl(name, "print(cdp)").stdout == "None\n"
        recorded = json.loads((session_dir / "state.json").read_text())
        recorded["apps"]["fakecdp"] = {
            "unit": recorded["prefix"] + "-app-fakecdp.service",
            "cdp_port": 9,
        }
        (session_dir / "state.json").write_text(json.dumps(recorded))
        assert repl(name, "print(cdp.port)").stdout == "9\n"
        # F4: driving the browser through cdp retires screenshot references.
        fake = directory / "agent-browser"
        fake.write_text(FAKE_AGENT_BROWSER)
        fake.chmod(0o700)
        snapshot = session_dir / "ocr-snapshot.json"
        driven = repl(
            name,
            "import os\n"
            "screenshot(targets='text')\n"
            f"print(os.path.exists({str(snapshot)!r}))\n"
            f"cdp.binary = {str(fake)!r}\n"
            "print(cdp.run('click', '@e1').strip())\n"
            f"print(os.path.exists({str(snapshot)!r}))\n",
        )
        assert driven.stdout == "True\nfake click\nFalse\n", driven
        print("PASS cdp is bound and cdp.run retires references", flush=True)

        # F5: a chatty spawned child is never blocked on an unread pipe.
        done = directory / "spawn-done"
        spawned = repl(
            name,
            "import os, time\n"
            f"p = spawn('sh', '-c', 'head -c 200000 /dev/zero >&2; touch {done}; sleep 100')\n"
            "start = time.monotonic()\n"
            f"while not os.path.exists({str(done)!r}) and time.monotonic() - start < 5:\n"
            "    time.sleep(0.05)\n"
            f"print(os.path.exists({str(done)!r}), os.path.getsize(p.log))\n"
            "p.kill()\n",
        )
        assert spawned.stdout == "True 200000\n", spawned
        print("PASS spawn logs output instead of blocking on a pipe", flush=True)

        # L4 and L5: relative paths use the caller's cwd; child output returns.
        caller = directory / "caller"
        caller.mkdir()
        relative = repl(
            name,
            "import subprocess\n"
            "subprocess.run(['echo', 'from-child'])\n"
            "print(screenshot(targets=None, output='relative.png')['image'])\n",
            cwd=caller,
        )
        assert (caller / "relative.png").is_file(), relative
        assert relative.stdout == f"from-child\n{caller / 'relative.png'}\n", relative
        print("PASS relative paths and subprocess output follow the caller", flush=True)

        # R2: ocr(image=relative) reads the caller's file, not the worker's cwd.
        font = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans-serif"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for word in ("Alpha", "Bravo"):
            place = directory / word.lower()
            place.mkdir()
            subprocess.run(
                [
                    "magick",
                    "-size",
                    "420x120",
                    "xc:white",
                    "-font",
                    font,
                    "-fill",
                    "black",
                    "-pointsize",
                    "56",
                    "-annotate",
                    "+20+85",
                    word,
                    "label.png",
                ],
                cwd=place,
                check=True,
            )
            read = repl(
                name, "print([b['text'] for b in ocr(image='label.png')])", cwd=place
            )
            assert word in read.stdout, (word, read.stdout)
        print("PASS ocr(image=relative) reads the caller's file", flush=True)

        # R3: output that grows when JSON-escaped is cut, not dropped.
        flood = repl(name, "print('\\x01' * 3_000_000)\nprint('after')")
        assert "[output truncated" in flood.stdout, flood.stdout[-200:]
        assert len(flood.stdout) < 9 * 1024 * 1024
        assert repl(name, "print('é' * 3)").stdout == "ééé\n"
        print("PASS escaped output is truncated to fit the reply", flush=True)

        marker = directory / "child.pid"
        repl(
            name,
            f"c = spawn('sleep', '1000')\nopen({str(marker)!r}, 'w').write(str(c.pid))",
        )
        child = int(marker.read_text())
        assert Path(f"/proc/{child}").exists()
        state = json.loads((session_dir / "state.json").read_text())
        repl_unit = state["prefix"] + "-repl.service"
        assert repl_unit in state["units"]
        command("stop", name)
        assert not session_dir.exists()
        alive = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", repl_unit], check=False
        )
        assert alive.returncode != 0, "repl service survived stop"
        assert not Path(f"/proc/{child}").exists(), "spawned child survived stop"
        print("PASS stop ends the repl and its children", flush=True)

        if host_before is not None:
            host_after = probe()
            assert host_after == host_before, (
                "host focus, cursor or clipboard changed",
                {key: host_before[key] == host_after[key] for key in host_before},
            )
            print("PASS host focus, cursor and clipboard digest unchanged", flush=True)
    finally:
        if session_dir.exists():
            command("stop", name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-repl-check-") as temporary:
        main(Path(temporary))
