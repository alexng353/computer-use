"""REPL lifetime: a cell never outlives the lock, and state is never silently lost.

Covers a cell that ignores its timeout interrupt after launching a unit (F1),
the CLI killed by SIGTERM or SIGKILL mid-cell (F2), replacement notices (F2),
stop during a running cell, malformed and stalled requests (L1, L2).
"""

import fcntl
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from native_ocr import CLI, STATE, command, wait_until


def locked(name):
    """True while some process holds the session's interaction lock."""
    with (STATE / ".locks" / (name + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def background(name, code, *extra):
    return subprocess.Popen(
        [sys.executable, str(CLI), "repl", name, *extra, "--code", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def state(name):
    return json.loads((STATE / name / "state.json").read_text())


def mouse(name):
    shell = command("input", name, "--", "getmouselocation", "--shell").stdout
    values = dict(line.split("=", 1) for line in shell.splitlines() if "=" in line)
    return int(values["X"]), int(values["Y"])


def unit_active(unit):
    return (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit], check=False
        ).returncode
        == 0
    )


# The cell catches the interrupt and keeps running for 1.5 s, so the lock must
# stay held after the CLI is gone, and the input must never be sent.
SLOW_CELL = """
import time
try:
    time.sleep(4)
except KeyboardInterrupt:
    time.sleep(1.5)
    raise
xdotool("mousemove", {x}, {y})
"""


def check_killed_cli(name, kill_signal, x, y):
    command("repl", name, "--code", "counter = 7")
    process = background(name, SLOW_CELL.format(x=x, y=y))
    wait_until(lambda: locked(name), "cell holds the lock")
    time.sleep(1)
    process.send_signal(kill_signal)
    time.sleep(0.5)
    assert locked(name), f"lock released while the cell ran after {kill_signal!r}"
    _, stderr = process.communicate(timeout=15)
    deadline = time.monotonic() + 10
    while locked(name):
        assert time.monotonic() < deadline, "cell outlived its interrupt"
        time.sleep(0.05)
    time.sleep(3)
    assert mouse(name) != (x, y), "the cell sent input after its CLI died"
    kept = command("repl", name, "--code", "print(counter)")
    assert kept.stdout == "7\n" and "state lost" not in kept.stderr, kept
    return process.returncode, stderr


def main(directory):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-replife-" + secrets.token_hex(3)
    session_dir = STATE / name
    try:
        command("start", name, "--size", "800x600", "--no-accessibility")

        # F1: the forced restart must keep what the cell recorded.
        cell = (
            "launch('orphan', 'sleep', '1000')\n"
            "import signal, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(30)\n"
        )
        forced = command("repl", name, "--timeout", "1", "--code", cell, ok=False)
        assert "ignored its interrupt" in forced.stderr, forced.stderr
        assert "state lost" in forced.stderr, forced.stderr
        recorded = state(name)
        orphan = recorded["apps"]["orphan"]["unit"]
        assert orphan in recorded["units"], recorded["units"]
        assert recorded["prefix"] + "-repl.service" not in recorded["units"]
        assert unit_active(orphan), "the cell's unit should keep running, owned"
        fresh = command("repl", name, "--code", "print('fresh')")
        assert fresh.stdout == "fresh\n", fresh
        print("PASS forced restart keeps units the cell recorded", flush=True)

        # F2a and F2b: SIGTERM is handled by the CLI, SIGKILL by the server.
        returncode, stderr = check_killed_cli(name, signal.SIGTERM, 11, 13)
        assert returncode != 0 and "received SIGTERM" in stderr, stderr
        print(
            "PASS SIGTERM keeps the lock until the cell ends; no late input", flush=True
        )
        check_killed_cli(name, signal.SIGKILL, 17, 19)
        print(
            "PASS SIGKILL: the server ends the cell and holds the lock meanwhile",
            flush=True,
        )

        # A cell that ignores the server's interrupt ends with its REPL, and
        # the next call says so instead of silently starting over.
        stubborn = background(
            name,
            "import signal, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(60)\n",
        )
        wait_until(lambda: locked(name), "stubborn cell holds the lock")
        time.sleep(1)
        stubborn.kill()
        stubborn.wait(timeout=10)
        time.sleep(2)
        assert locked(name), "lock released before the stubborn cell ended"
        deadline = time.monotonic() + 15
        while locked(name):
            assert time.monotonic() < deadline, "stubborn cell outlived its REPL"
            time.sleep(0.05)
        after_crash = command("repl", name, "--code", "print('again')")
        assert after_crash.stdout == "again\n", after_crash
        assert "previous state lost (its service had stopped)" in after_crash.stderr, (
            after_crash.stderr
        )
        print("PASS a REPL that had to exit is replaced with a notice", flush=True)
        command("repl", name, "--code", "counter = 7")

        reset = command("repl", name, "--reset", "--code", "print('reset')")
        assert "previous state lost (--reset)" in reset.stderr, reset.stderr
        assert (
            "NameError" in command("repl", name, "--code", "counter", ok=False).stderr
        )
        print("PASS --reset says that the previous state is lost", flush=True)

        # L1 and L2: malformed and stalled clients cannot break the server.
        socket_path = str(session_dir / "repl.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(10)
            raw.connect(socket_path)
            raw.sendall(b"[]\n")
            reply = json.loads(raw.makefile("rb").readline())
        assert "error" in reply, reply
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stalled:
            stalled.connect(socket_path)
            started = time.monotonic()
            after = command("repl", name, "--code", "print('after')", timeout=30)
        assert after.stdout == "after\n" and "state lost" not in after.stderr, after
        assert time.monotonic() - started < 10
        large = subprocess.run(
            [sys.executable, str(CLI), "repl", name],
            input="#" * (17 * 1024 * 1024),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert large.returncode != 0 and "too large" in large.stderr, large.stderr
        assert command("repl", name, "--code", "print(2)").stdout == "2\n"
        print("PASS malformed, stalled and oversized requests get errors", flush=True)

        # A REPL busy with a cell (here sent without the lock) is never replaced.
        command("repl", name, "--code", "kept = 'yes'")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(30)
            raw.connect(socket_path)
            raw.sendall(b'{"action": "run", "code": "import time; time.sleep(8)"}\n')
            busy = command("repl", name, "--code", "print(1)", ok=False, timeout=30)
            assert "not answering" in busy.stderr, busy.stderr
            raw.makefile("rb").readline()
        still = command("repl", name, "--code", "print(kept)")
        assert still.stdout == "yes\n" and "state lost" not in still.stderr, still
        print("PASS a busy REPL is refused, not replaced", flush=True)

        # Stop during a running cell ends the cell, its children and the session.
        marker = directory / "child.pid"
        process = background(
            name,
            f"child = spawn('sleep', '1000')\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            "import time\ntime.sleep(60)\n",
        )
        wait_until(lambda: marker.exists() and marker.read_text(), "spawned child")
        child = int(marker.read_text())
        units = state(name)["units"]
        command("stop", name, timeout=60)
        process.wait(timeout=30)
        assert process.returncode != 0 and "stopping" in process.stderr.read()
        assert not session_dir.exists()
        assert not Path(f"/proc/{child}").exists(), "spawned child survived stop"
        assert not any(unit_active(unit) for unit in units)
        assert not unit_active(orphan)
        print(
            "PASS stop mid-cell ends the cell, its children and every unit", flush=True
        )
    finally:
        if session_dir.exists():
            command("stop", name, timeout=60)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-replife-") as temporary:
        main(Path(temporary))
