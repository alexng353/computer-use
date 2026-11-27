"""Exercise the persistent session REPL against a real zenity dialog on Xvfb.

One REPL call opens zenity, types three lines, presses OK and asserts the
dialog's output; the same task through single CLI commands needs at least five
calls. Also checks state persistence, error and timeout recovery, stdin cells,
cleanup on stop, and that the host focus, cursor and clipboard stay unchanged.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from host_isolation import probe
from native_ocr import CLI, STATE, command, wait_until

LINES = ["first line", "second line", "third line"]

# GTK4 zenity reports every AT-SPI extent at 0,0 without a window manager, so
# the OK button is targeted by its OCR text.
ZENITY_CELL = """
p = spawn("zenity", "--text-info", "--editable", "--title=Repl notes",
          "--width=600", "--height=400")
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


def images(stdout):
    return [
        line.removeprefix("[image] ")
        for line in stdout.splitlines()
        if line.startswith("[image] ")
    ]


def piped(name, *args, stdin):
    return subprocess.run(
        [sys.executable, str(CLI), *args[:1], name, *args[1:]],
        stdin=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def cli_baseline(name, directory, session_dir):
    """Run the zenity task one CLI command per step; return the call count."""
    calls = 0

    def cli(*args):
        nonlocal calls
        calls += 1
        return command(*args)

    output = directory / "zenity-cli.txt"
    cli(
        "launch",
        name,
        "--id",
        "notes",
        "--",
        "sh",
        "-c",
        "zenity --text-info --editable --title='CLI notes' "
        f"--width=600 --height=400 > {output}",
    )
    found = []

    def listed():
        found[:] = [
            w
            for w in json.loads(cli("windows", name).stdout)
            if w["title"] == "CLI notes"
        ]
        return found

    wait_until(listed, "CLI zenity window")
    cli("input", name, "--", "windowfocus", str(found[0]["id"]))
    text = directory / "lines.txt"
    text.write_text("\n".join(LINES))
    with text.open() as stdin:
        calls += 1
        piped(name, "clipboard", "set", stdin=stdin)
    cli("input", name, "--", "key", "--clearmodifiers", "ctrl+v")
    # An agent recaptures when click verification sees the dialog still redrawing.
    for _ in range(4):
        cli(
            "screenshot",
            name,
            "--targets",
            "text",
            "--output",
            str(directory / "cli.png"),
        )
        snapshot = json.loads((session_dir / "ocr-snapshot.json").read_text())
        ok = next(t for t in snapshot["targets"] if t["text"] == "OK")
        calls += 1
        clicked = subprocess.run(
            [sys.executable, str(CLI), "click", name, "@" + ok["ref"]],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if clicked.returncode == 0:
            break
        assert "changed" in clicked.stderr, clicked.stderr
    wait_until(lambda: output.exists() and output.read_text(), "CLI output")
    assert output.read_text().splitlines() == LINES
    return calls


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

        result = command("repl", name, "--code", ZENITY_CELL, timeout=120)
        assert f"zenity: {LINES}" in result.stdout, result
        shots = images(result.stdout)
        assert len(shots) == 1 and Path(shots[0]).is_file(), result.stdout
        print("PASS one repl call drives zenity end to end", flush=True)

        observed = command("repl", name, "--code", OBSERVE_CELL, timeout=120)
        assert "observed True 0" in observed.stdout, observed
        assert len(images(observed.stdout)) == 1, observed.stdout
        print("PASS a11y(), ocr() and zoom() read a live dialog", flush=True)

        calls = cli_baseline(name, directory, session_dir)
        assert calls >= 5, calls
        print(
            f"PASS the same task takes {calls} CLI calls without the repl", flush=True
        )

        command("repl", name, "--code", "counter = 41")
        assert command("repl", name, "--code", "counter + 1").stdout.strip() == "42"
        failed = command("repl", name, "--code", "1 / 0", ok=False)
        assert "ZeroDivisionError" in failed.stderr, failed.stderr
        assert command("repl", name, "--code", "print(counter)").stdout == "41\n"
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
            assert piped(name, "repl", stdin=stdin).stdout == "82\n"
        print("PASS state persists across calls, errors and timeouts", flush=True)

        state = json.loads((session_dir / "state.json").read_text())
        repl_unit = state["prefix"] + "-repl.service"
        assert repl_unit in state["units"]
        command("stop", name)
        assert not session_dir.exists()
        alive = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", repl_unit], check=False
        )
        assert alive.returncode != 0, "repl service survived stop"
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
