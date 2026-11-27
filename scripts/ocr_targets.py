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
import accessibility_targets

RUNTIME = Path.home() / ".local/share/computer-use/ocr-venv"
SCRIPTS = Path(__file__).resolve().parent
PREFIXES = "abcdefg"
WORKERS = {"text": "ocr", "icons": "icons", "accessibility": "accessibility"}
# `all` merges every available source; earlier sources win a duplicate.
MERGE_ORDER = ("accessibility", "text", "icons")
# Two boxes are one control when they overlap like this (UFO2-style IoU), or
# when one sits almost wholly inside the other (OCR text inside its button).
DUPLICATE_IOU = 0.5
DUPLICATE_CONTAINMENT = 0.8


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
    return Path(state["directory"]) / (WORKERS[target_mode] + ".sock")


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
    if target_mode not in WORKERS:
        raise ValueError("Target mode must be text, icons or accessibility")
    if target_mode == "accessibility":
        if state.get("accessibility_bus") != accessibility_targets.bus_address(state):
            raise RuntimeError(
                "Launch an app or start a session with --accessibility first; existing apps need relaunching"
            )
        if not accessibility_targets.services_alive(state):
            raise RuntimeError(
                "Accessibility bus stopped; recreate the session and relaunch its apps"
            )
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
    if target_mode == "accessibility":
        accessibility_targets.check_dependencies()
        python = accessibility_targets.PYTHON
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
    unit = state["prefix"] + "-" + WORKERS[target_mode] + ".service"
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


def area(bounds):
    return max(0, bounds[2] - bounds[0]) * max(0, bounds[3] - bounds[1])


def duplicate(a, b):
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return False
    shared = width * height
    union = area(a) + area(b) - shared
    return (
        shared / union >= DUPLICATE_IOU
        or shared / min(area(a), area(b)) >= DUPLICATE_CONTAINMENT
    )


def merge(groups):
    """Keep each target unless an earlier-kept one covers the same control."""
    kept = []
    for targets in groups:
        for target in targets:
            if not any(duplicate(target["bounds"], k["bounds"]) for k in kept):
                kept.append(target)
    return kept


def available_sources(state):
    ready = {
        "accessibility": state.get("accessibility_bus")
        == accessibility_targets.bus_address(state)
        and accessibility_targets.services_alive(state),
        # Required: ensure_worker explains a missing OCR runtime.
        "text": True,
        "icons": (icon_detector.RUNTIME / "bin/python").exists()
        and icon_detector.valid_model(icon_detector.MODEL),
    }
    return [mode for mode in MERGE_ORDER if ready[mode]]


def recognize_all(state, start_service, source, output, prefix):
    """One badge set from every available source, de-duplicated by overlap."""
    groups, used, skipped = [], [], {}
    with tempfile.TemporaryDirectory(dir=state["directory"]) as scratch:
        for mode in available_sources(state):
            try:
                ensure_worker(state, start_service, target_mode=mode)
                result = request(
                    state,
                    {
                        "action": "recognize",
                        "source": str(source),
                        "output": str(Path(scratch) / f"{mode}.png"),
                        "prefix": prefix,
                    },
                    target_mode=mode,
                )
            except RuntimeError as error:
                # One source failing (no focused accessible window, say) must
                # not hide what the others found.
                if mode == "text":
                    raise
                skipped[mode] = str(error)
                continue
            used.append(mode)
            groups.append(
                [
                    {**target, "kind": target.get("kind", mode)}
                    for target in result["targets"]
                ]
            )
    targets = merge(groups)
    targets.sort(key=lambda item: (item["bounds"][1], item["bounds"][0]))
    for index, target in enumerate(targets, start=1):
        target["ref"] = prefix + str(index)
    rendered = request(
        state,
        {
            "action": "annotate",
            "source": str(source),
            "output": str(output),
            "targets": targets,
        },
        target_mode="text",
    )
    return {
        **rendered,
        "targets": targets,
        "sources": used,
        **({"skipped": skipped} if skipped else {}),
    }


def verifier(snapshot, target):
    """Worker that verifies a target: semantics for accessibility, pixels otherwise."""
    mode = snapshot.get("target_mode", "text")
    if mode != "all":
        return mode
    return "accessibility" if target.get("kind") == "accessibility" else "text"


def screenshot(state, output, capture, start_service, raw=False, target_mode="text"):
    invalidate(state)
    if raw:
        capture(output)
        return
    if target_mode != "all":
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
        if target_mode == "all":
            result = recognize_all(
                state, start_service, pending_source, pending_output, prefix
            )
        else:
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
    invalidate(state)
    target_mode = verifier(snapshot, target)
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
                "target": target,
            },
            target_mode=target_mode,
        )
    if not verified["matches"]:
        raise RuntimeError(
            "Target pixels changed or accessibility target changed; take a fresh screenshot before clicking"
        )
    native_click(*target["center"])
