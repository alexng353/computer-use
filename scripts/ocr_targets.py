"""Screenshot references and the private, session-owned OCR worker client."""

import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import icon_detector

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


def socket_path(state, target_mode):
    return Path(state["directory"]) / (
        "icons.sock" if target_mode == "icons" else "ocr.sock"
    )


def request(state, message, timeout=60, target_mode="text"):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path(state, target_mode)))
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as response:
            line = response.readline(4 * 1024 * 1024)
    result = json.loads(line)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def ensure_worker(state, start_service, target_mode="text"):
    if target_mode not in ["text", "icons"]:
        raise ValueError("Target mode must be text or icons")
    if len(os.fsencode(socket_path(state, target_mode))) >= 108:
        raise RuntimeError(
            "Session path is too long for the OCR socket; use a shorter name or --raw"
        )
    try:
        request(state, {"action": "ping"}, timeout=1, target_mode=target_mode)
        return
    except (OSError, ValueError):
        pass
    python = (
        icon_detector.RUNTIME if target_mode == "icons" else RUNTIME
    ) / "bin/python"
    if not python.exists():
        raise RuntimeError(
            "Targets are not installed; run computer-use "
            + ("setup-icons" if target_mode == "icons" else "setup-ocr")
            + ", or screenshot --raw"
        )
    if target_mode == "icons" and not icon_detector.valid_model(icon_detector.MODEL):
        raise RuntimeError(
            "Icon model missing or invalid; run computer-use setup-icons"
        )
    unit = state["prefix"] + (
        "-icons.service" if target_mode == "icons" else "-ocr.service"
    )
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
            "--targets",
            target_mode,
        ],
    )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            request(state, {"action": "ping"}, timeout=1, target_mode=target_mode)
            return
        except (OSError, ValueError):
            time.sleep(0.05)
    raise RuntimeError(f"OCR worker did not start; inspect journalctl --user -u {unit}")


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def screenshot(state, output, capture, start_service, raw=False, target_mode="text"):
    invalidate(state)
    if raw:
        capture(output)
        return
    ensure_worker(state, start_service, target_mode=target_mode)
    directory = Path(state["directory"])
    counter = directory / "ocr-generation.json"
    generation = json.loads(counter.read_text()) + 1 if counter.exists() else 0
    atomic_json(counter, generation)
    prefix = PREFIXES[generation % len(PREFIXES)]
    source = directory / "ocr-screen.png"
    # A timed-out worker may still finish after a later capture in the other mode.
    # Only this client can publish to the shared source and caller's output path.
    with (
        tempfile.TemporaryDirectory(dir=directory, prefix=".capture-") as temporary,
        tempfile.TemporaryDirectory(dir=output.parent, prefix=".capture-") as preview,
    ):
        pending_source = Path(temporary) / "source.png"
        pending_output = Path(preview) / ("preview" + output.suffix)
        capture(pending_source)
        result = request(
            state,
            {
                "action": "recognize",
                "source": str(pending_source),
                "output": str(pending_output),
                "prefix": prefix,
            },
            target_mode=target_mode,
        )
        pending_source.replace(source)
        pending_output.replace(output)
    atomic_json(
        directory / "ocr-snapshot.json",
        {
            "prefix": prefix,
            "source": str(source),
            "image": str(output),
            "target_mode": target_mode,
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
    target_mode = snapshot.get("target_mode", "text")
    ensure_worker(state, start_service, target_mode=target_mode)
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
            target_mode=target_mode,
        )
    invalidate(state)
    if not verified["matches"]:
        raise RuntimeError(
            "Target pixels changed; take a fresh screenshot before clicking"
        )
    native_click(*target["center"])
