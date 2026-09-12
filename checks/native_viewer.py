"""Verify the packaged viewer's HTTP frames, virtual cursor, and owned cleanup."""

import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

CLI = Path(__file__).resolve().parents[1] / "scripts/computer_use.py"


def command(*args):
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def get(url, headers=None):
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers or {}), timeout=3
    ) as response:
        return response.read()


def main():
    name = "check-viewer-" + secrets.token_hex(5)
    state = json.loads(command("start", name, "--size", "640x480"))
    url = None
    try:
        viewer = json.loads(command("viewer", name))
        url = viewer["url"]
        assert urlsplit(url).hostname == "127.0.0.1"
        assert json.loads(command("viewer", name))["url"] == url
        assert b"read only" in get(url)
        first = get(url + "frame")
        assert first.startswith(b"\xff\xd8") and first.endswith(b"\xff\xd9")
        command("input", name, "--", "mousemove", "35", "45")
        cursor = json.loads(get(url + "cursor"))
        assert cursor == {"x": 35, "y": 45, "width": 640, "height": 480}, cursor
        deadline = time.monotonic() + 5
        while get(url + "frame") == first:
            assert time.monotonic() < deadline, "Viewer frames did not update"
            time.sleep(0.1)
        for headers in ({"Host": "attacker.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            try:
                get(url + "frame", headers)
            except urllib.error.HTTPError as error:
                assert error.code == 403
            else:
                raise AssertionError("Viewer accepted a foreign browser origin")
    finally:
        command("stop", name)
    assert not Path(state["directory"]).exists()
    if url:
        port = urlsplit(url).port
        with socket.socket() as client:
            client.settimeout(2)
            assert client.connect_ex(("127.0.0.1", port)) != 0, "Viewer survived stop"
    print(
        "PASS packaged viewer frames, cursor, origin checks, repeat open, and cleanup"
    )


if __name__ == "__main__":
    main()
