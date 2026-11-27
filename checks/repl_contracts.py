"""REPL client contracts against a fake server, without a desktop.

A cell that finishes after the timeout fired but before its interrupt landed is
reported as finished, not as interrupted (L3); the lock travels with the request.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import computer_use
import repl


def fake_server(path, replies, delay):
    """Answer pings at once and each run request with the next reply after `delay`."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    received = []

    def loop():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                message, lock = repl.read_request(connection)
                received.append((message.get("action"), lock is not None))
                if lock is not None:
                    os.close(lock)
                if message["action"] == "ping":
                    reply = {"ready": True}
                else:
                    time.sleep(delay)
                    reply = replies.pop(0)
                connection.sendall(json.dumps(reply).encode() + b"\n")

    threading.Thread(target=loop, daemon=True).start()
    return server, received


def main(base):
    computer_use.ROOT = base
    directory = base / "fake"
    directory.mkdir()
    state = {
        "name": "fake",
        "directory": str(directory),
        "prefix": "computer-use-contract-fake-000000",
        "units": ["computer-use-contract-fake-000000-repl.service"],
        "apps": {},
    }
    computer_use.save(state)
    finished = {"stdout": "done\n", "images": [], "error": None}
    server, received = fake_server(
        repl.socket_path(state),
        [
            {**finished, "interrupted": False},
            {**finished, "error": "KeyboardInterrupt\n", "interrupted": True},
        ],
        delay=0.6,
    )

    def no_start(*_args):
        raise AssertionError("a ready REPL must not be replaced")

    with server, tempfile.TemporaryFile() as lock:
        late = repl.call(state, {"code": "x"}, no_start, lock, lambda: False, 0.2)
        assert late["error"] is None and late["stdout"] == "done\n", late
        print("PASS a cell that finished before its interrupt is not reported as one")
        cut = repl.call(state, {"code": "x"}, no_start, lock, lambda: False, 0.2)
        assert cut["error"].startswith("Cell interrupted because its 0.2 s timeout")
        assert ("run", True) in received and ("ping", False) in received, received
        print("PASS interrupted cells say so; run requests carry the lock")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cu-repl-contract-") as temporary:
        main(Path(temporary))
