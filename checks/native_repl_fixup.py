"""Review fix-up 2b for the REPL: inherited locks, late launches, limits.

N1  a background child of a cell never inherits the session lock, and `stop`
    still finishes when something in the REPL holds the lock anyway;
R1  a unit launched while the REPL is being stopped stays recorded;
R4  the server refuses an oversized request itself;
R5  a malformed field gets an error reply and the REPL keeps its state.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from native_ocr import CLI, STATE, command
from native_repl_lifecycle import locked, state, unit_active


def run_bytes(name, code):
    """Run a cell given as bytes (argv may hold non-UTF-8); return raw output."""
    return subprocess.run(
        [sys.executable, str(CLI), "repl", name, "--code", code],
        capture_output=True,
        timeout=120,
        check=False,
    )


def check_2c(name, directory):
    """Fix-up 2c: surrogates, forked children, kept output, reply budget."""
    state_file = STATE / name / "state.json"
    command("repl", name, "--code", "kept = 'yes'")

    # C2-M1: surrogates in errors, image paths and code never kill the server.
    raised = run_bytes(
        name,
        b"raise ValueError('missing ' + b'\\xff.png'.decode('utf-8', 'surrogateescape'))",
    )
    assert raised.returncode == 1, raised
    assert b"ValueError: missing \xff.png" in raised.stderr, raised.stderr[-300:]
    odd = os.fsencode(directory) + b"/\xff.png"
    with open(odd, "wb") as image:
        image.write(b"png")
    shown = run_bytes(
        name, b"import os\ndisplay(os.fsdecode(" + repr(odd).encode() + b"))"
    )
    assert shown.returncode == 0 and b"\xff.png" in shown.stdout, shown
    # Non-UTF-8 code cannot compile, but it reaches the REPL and fails as a cell.
    argv = run_bytes(name, b"print(len('\xff'))")
    assert argv.returncode == 1 and b"surrogates not allowed" in argv.stderr, argv
    after = command("repl", name, "--code", "print(kept)")
    assert after.stdout == "yes\n" and "state lost" not in after.stderr, after
    print("PASS surrogates in errors, image paths and code keep the REPL", flush=True)

    # C2-L2 / L5: deep JSON, and refresh failures, get error replies.
    deep = raw_request(name, b"[" * 100_000 + b"]" * 100_000 + b"\n")
    assert "error" in deep, deep
    original = state_file.read_text()
    for broken in (
        {k: v for k, v in json.loads(original).items() if k != "prefix"},
        {**json.loads(original), "prefix": "computer-use-replaced-000000"},
    ):
        state_file.write_text(json.dumps(broken))
        try:
            reply = raw_request(name, b'{"action": "run", "code": "x = 1"}\n')
        finally:
            state_file.write_text(original)
        assert "error" in reply, reply
    assert command("repl", name, "--code", "print(kept)").stdout == "yes\n"
    print("PASS deep JSON and refresh failures get errors; state survives", flush=True)

    # C2-M2: a forked child without exec never keeps the session lock.
    pidfile = directory / "forked.pid"
    command(
        "repl",
        name,
        "--code",
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        f"open({str(pidfile)!r}, 'w').write(str(pid))\n",
    )
    forked = int(pidfile.read_text())
    assert alive(forked)
    time.sleep(1)
    assert not locked(name), "a forked child kept the session lock"
    os.kill(forked, 9)
    print("PASS a forked child does not hold the lock", flush=True)

    # C2-L1: late output appends after the cell's own output.
    late = command(
        "repl",
        name,
        "--code",
        "import os\nos.system('(sleep 1; echo LATE) &')\nprint('A' * 9_000_000)\n",
        timeout=120,
    )
    assert "[output truncated" in late.stdout
    time.sleep(2)
    logs = STATE / name / "spawn-logs"
    newest = max(logs.glob("cell-*.out"), key=lambda path: path.stat().st_mtime)
    with newest.open("rb") as kept_file:
        head = kept_file.read(10)
        kept_file.seek(-5, os.SEEK_END)
        tail = kept_file.read()
    assert head == b"A" * 10 and tail == b"LATE\n", (head, tail)
    for _ in range(25):
        command("repl", name, "--code", "pass")
    assert len(list(logs.glob("cell-*.out"))) <= 20
    print("PASS late output appends; old cell files are not kept forever", flush=True)

    # C2-L3 / L4 / L5: one byte budget, filled as far as it goes.
    huge = command(
        "repl",
        name,
        "--code",
        "raise RuntimeError('x' * 20_000_000)",
        ok=False,
        timeout=120,
    )
    assert "truncated" in huge.stderr and len(huge.stderr) < 2_000_000, len(huge.stderr)
    mixed = command(
        "repl",
        name,
        "--code",
        "print('A' * 4_000_000 + '\\x01' * 4_000_000)",
        timeout=120,
    )
    assert "[output truncated" in mixed.stdout and len(mixed.stdout) > 4_500_000, len(
        mixed.stdout
    )
    accents = command("repl", name, "--code", "print('é' * 4_000_000)", timeout=120)
    assert accents.stdout == "é" * 4_000_000 + "\n", len(accents.stdout)
    assert command("repl", name, "--code", "print(kept)").stdout == "yes\n"
    print("PASS the reply fits one budget and keeps as much output as fits", flush=True)


def raw_request(name, payload, timeout=30):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
        raw.settimeout(timeout)
        raw.connect(str(STATE / name / "repl.sock"))
        try:
            raw.sendall(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return json.loads(raw.makefile("rb").readline())


def alive(pid):
    return Path(f"/proc/{pid}").exists()


def main(directory):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-fixup-" + secrets.token_hex(3)
    session_dir = STATE / name
    lock_path = STATE / ".locks" / (name + ".lock")
    try:
        command("start", name, "--size", "800x600", "--no-accessibility")

        # N1: `cmd &` from a cell must not take the session lock with it.
        pidfile = directory / "background.pid"
        command(
            "repl",
            name,
            "--code",
            f"import os\nos.system('sleep 300 & echo $! > {pidfile}')",
        )
        background = int(pidfile.read_text())
        assert alive(background)
        time.sleep(1)
        assert not locked(name), "a background child inherited the session lock"
        print("PASS a background child of a cell does not hold the lock", flush=True)

        # R5: malformed fields are refused; the REPL and its state survive.
        command("repl", name, "--code", "kept = 'yes'")
        bad = raw_request(name, b'{"action": "run", "code": "x = 1", "image_dir": 5}\n')
        assert "error" in bad and "image_dir" in bad["error"], bad
        assert command("repl", name, "--code", "print(kept)").stdout == "yes\n"
        print("PASS a malformed field gets an error and state survives", flush=True)

        # R4: the server enforces its own size limit on a complete line.
        huge = b'{"action": "run", "code": "' + b"#" * (17 * 1024 * 1024) + b'"}\n'
        refused = raw_request(name, huge, timeout=60)
        assert "too large" in refused.get("error", ""), refused
        assert command("repl", name, "--code", "print(kept)").stdout == "yes\n"
        print("PASS the server refuses an oversized request itself", flush=True)

        # R1: a unit launched while the REPL is stopped stays owned.
        cell = (
            "import os, signal, time\n"
            "def late(_signum, _frame):\n"
            "    launch('late', 'sleep', '1000')\n"
            "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, late)\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(60)\n"
        )
        forced = command("repl", name, "--timeout", "1", "--code", cell, ok=False)
        assert "state lost" in forced.stderr, forced.stderr
        recorded = state(name)
        assert "late" in recorded["apps"], recorded["apps"]
        late = recorded["apps"]["late"]["unit"]
        assert late in recorded["units"] and unit_active(late)
        print("PASS a unit launched during REPL shutdown stays recorded", flush=True)

        check_2c(name, directory)

        # N1 hardening: something in the REPL holds the lock; stop still ends it.
        holder = directory / "holder.pid"
        script = directory / "holder.py"
        script.write_text(
            "import fcntl, sys, time\n"
            "lock = open(sys.argv[1], 'a')\n"
            "fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "time.sleep(300)\n"
        )
        command(
            "repl",
            name,
            "--code",
            "import subprocess, sys\n"
            f"p = subprocess.Popen([sys.executable, {str(script)!r}, {str(lock_path)!r}])\n"
            f"open({str(holder)!r}, 'w').write(str(p.pid))\n",
        )
        holder_pid = int(holder.read_text())
        deadline = time.monotonic() + 5
        while not locked(name):
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.05)
        units = state(name)["units"]
        started = time.monotonic()
        stopped = command("stop", name, timeout=60)
        assert "stopped the REPL" in stopped.stderr, stopped.stderr
        assert time.monotonic() - started < 40
        assert not session_dir.exists()
        assert not alive(holder_pid) and not alive(background)
        assert not any(unit_active(unit) for unit in units)
        assert not unit_active(late)
        print(
            "PASS stop breaks a lock held inside the REPL; nothing survives", flush=True
        )
    finally:
        if session_dir.exists():
            command("stop", name, timeout=90)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-fixup-") as temporary:
        main(Path(temporary))
