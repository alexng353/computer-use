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
import tempfile
import time
from pathlib import Path

from native_ocr import STATE, command
from native_repl_lifecycle import locked, state, unit_active


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
