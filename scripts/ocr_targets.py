"""Screenshot references and the private, session-owned OCR worker client."""

import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

RUNTIME = Path.home() / ".local/share/computer-use/ocr-venv"
SCRIPTS = Path(__file__).resolve().parent
PREFIXES = "abcdefg"


def invalidate(state):
    (Path(state["directory"]) / "ocr-snapshot.json").unlink(missing_ok=True)


def setup():
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    if not (RUNTIME / "bin/python").exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(RUNTIME)], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            str(RUNTIME / "bin/python"),
            str(SCRIPTS / "ocr-requirements.lock"),
        ],
        check=True,
    )
    subprocess.run(
        [str(RUNTIME / "bin/python"), str(SCRIPTS / "ocr_worker.py"), "--warmup"],
        check=True,
    )


def request(state, message, timeout=60):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(Path(state["directory"]) / "ocr.sock"))
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as response:
            line = response.readline(4 * 1024 * 1024)
    result = json.loads(line)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def ensure_worker(state, start_service):
    if len(os.fsencode(Path(state["directory"]) / "ocr.sock")) >= 108:
        raise RuntimeError(
            "Session path is too long for the OCR socket; use a shorter name or --raw"
        )
    try:
        request(state, {"action": "ping"}, timeout=1)
        return
    except (OSError, ValueError):
        pass
    python = RUNTIME / "bin/python"
    if not python.exists():
        raise RuntimeError(
            "OCR is not installed; run computer-use setup-ocr, or screenshot --raw"
        )
    unit = state["prefix"] + "-ocr.service"
    # Restart a crashed worker without duplicating the session's ownership record.
    if unit in state["units"]:
        subprocess.run(
            ["systemctl", "--user", "stop", unit], check=False, capture_output=True
        )
        if (
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit], check=False
            ).returncode
            == 0
        ):
            raise RuntimeError(f"Could not stop OCR worker {unit}")
        state["units"].remove(unit)
    start_service(
        state,
        unit,
        [
            str(python),
            str(SCRIPTS / "ocr_worker.py"),
            "--session-dir",
            state["directory"],
        ],
    )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            request(state, {"action": "ping"}, timeout=1)
            return
        except (OSError, ValueError):
            time.sleep(0.05)
    raise RuntimeError(f"OCR worker did not start; inspect journalctl --user -u {unit}")


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def screenshot(state, output, capture, start_service, raw=False):
    invalidate(state)
    if raw:
        capture(output)
        return
    ensure_worker(state, start_service)
    directory = Path(state["directory"])
    counter = directory / "ocr-generation.json"
    generation = json.loads(counter.read_text()) + 1 if counter.exists() else 0
    atomic_json(counter, generation)
    prefix = PREFIXES[generation % len(PREFIXES)]
    source = directory / "ocr-screen.png"
    capture(source)
    result = request(
        state,
        {
            "action": "recognize",
            "source": str(source),
            "output": str(output),
            "prefix": prefix,
        },
    )
    atomic_json(
        directory / "ocr-snapshot.json",
        {
            "prefix": prefix,
            "source": str(source),
            "image": str(output),
            **result,
        },
    )


def query(state, reference):
    if not re.fullmatch(rf"@?[{PREFIXES}][1-9][0-9]*", reference):
        raise RuntimeError("Use a screenshot reference such as @a13")
    path = Path(state["directory"]) / "ocr-snapshot.json"
    if not path.exists():
        raise RuntimeError(
            "No current OCR references; take a fresh annotated screenshot"
        )
    snapshot = json.loads(path.read_text())
    reference = reference.removeprefix("@")
    if reference[0] != snapshot["prefix"]:
        raise RuntimeError(f"Stale reference @{reference}; use the current screenshot")
    target = next(
        (item for item in snapshot["targets"] if item["ref"] == reference), None
    )
    if target is None:
        raise RuntimeError(
            f"Unknown or stale reference @{reference}; use the current screenshot"
        )
    return snapshot, target


def click(state, reference, capture, native_click, start_service):
    snapshot, target = query(state, reference)
    ensure_worker(state, start_service)
    # A background redraw can happen even when no command has sent input.
    with tempfile.NamedTemporaryFile(dir=state["directory"], suffix=".png") as current:
        capture(Path(current.name))
        verified = request(
            state,
            {
                "action": "verify",
                "source": snapshot["source"],
                "current": current.name,
                "bounds": target["bounds"],
            },
        )
    invalidate(state)
    if not verified["matches"]:
        raise RuntimeError(
            "Target pixels changed; take a fresh screenshot before clicking"
        )
    native_click(*target["center"])
