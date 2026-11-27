"""Pixel helpers shared by the CLI (zoom, --settle) and the REPL (zoom, wait_stable)."""

import hashlib
import math
import os
import signal
import subprocess
import time
from pathlib import Path

# Crops smaller than this on their long edge are upscaled for reading.
ZOOM_TARGET = 800
ZOOM_MAX_SCALE = 8
# Settling waits for this much unchanged screen, up to the cap.
SETTLE_QUIET = 0.5
SETTLE_CAP = 5.0
SETTLE_MAX_MS = 30000
# Larger zoom output takes too long to render and is useless to view.
ZOOM_MAX_EDGE = 8192


class Cancelled(RuntimeError):
    """Settling stopped because the session is stopping."""


def settle_milliseconds(value):
    """argparse type for --settle: 1 to SETTLE_MAX_MS."""
    import argparse

    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= SETTLE_MAX_MS:
        raise argparse.ArgumentTypeError(
            f"--settle takes 1 to {SETTLE_MAX_MS} milliseconds, not {value!r}"
        )
    return number


# Only the end of magick's stderr is relayed; its last lines say what failed.
STDERR_TAIL = 4096


def magick(args, env, timeout=10.0, cancelled=None, label="magick"):
    """Run magick, bounded by timeout and ended at once when cancelled().

    The process runs in its own group, so a stop or timeout also reaps any
    helper it started. Returns stdout bytes; failures carry magick's stderr.
    """
    with subprocess.Popen(
        ["magick", *args],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + timeout
        while True:
            stopping = cancelled is not None and cancelled()
            if stopping or time.monotonic() >= deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                if stopping:
                    raise Cancelled("Capture cancelled because the session is stopping")
                raise RuntimeError(f"{label} did not finish within {timeout:g} s")
            try:
                stdout, stderr = process.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
    if process.returncode:
        reason = stderr[-STDERR_TAIL:].decode(errors="replace").strip()
        raise RuntimeError(
            f"{label} failed ({process.returncode}): {reason or 'no error output'}"
        )
    return stdout


def frame_digest(env, cancelled=None, timeout=10.0):
    raw = magick(
        ["import", "-window", "root", "-depth", "8", "rgb:-"],
        env,
        timeout=timeout,
        cancelled=cancelled,
        label="magick frame capture",
    )
    return hashlib.sha256(raw).digest()


def capture_to(env, output, cancelled=None, timeout=SETTLE_CAP):
    """Capture the screen to output, or leave no file at all."""
    output = Path(output)
    partial = output.with_name(f".partial-{output.name}")
    try:
        magick(
            ["import", "-window", "root", str(partial)],
            env,
            timeout=timeout,
            cancelled=cancelled,
            label="magick screen capture",
        )
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    return output


def wait_stable(
    env, timeout=SETTLE_CAP, quiet=SETTLE_QUIET, interval=0.05, cancelled=None
):
    """Poll frames until none changes for `quiet` seconds; False at the cap.

    A change that starts later than `quiet` after the action is missed, so
    delayed loads need a quiet window longer than their delay. `cancelled()`
    is checked on every poll and raises Cancelled when true.
    """
    if timeout < quiet:
        raise ValueError("timeout must be at least quiet, or it can never settle")

    def check():
        if cancelled is not None and cancelled():
            raise Cancelled("Settling cancelled because the session is stopping")

    check()
    # Each capture is bounded by the cap and ends at once on stop.
    last = frame_digest(env, cancelled, timeout=timeout)
    # Both clocks start after the first frame, so timeout == quiet can settle.
    deadline = time.monotonic() + timeout
    since = time.monotonic()
    while True:
        check()
        now = time.monotonic()
        if now - since >= quiet:
            return True
        if now >= deadline:
            return False
        time.sleep(interval)
        current = frame_digest(env, cancelled, timeout=timeout)
        if current != last:
            last, since = current, time.monotonic()


def settle(env, milliseconds, cancelled=None):
    """The CLI's --settle=MS: wait for MS of unchanged screen, capped."""
    quiet = milliseconds / 1000
    # A cap shorter than the quiet window could never report a settled screen.
    return wait_stable(
        env, timeout=max(SETTLE_CAP, 2 * quiet), quiet=quiet, cancelled=cancelled
    )


def zoom(env, box, output, scale=None, size=None):
    """Crop a fresh full-resolution capture to box (x0, y0, x1, y1, exclusive)."""
    x0, y0, x1, y1 = (int(value) for value in box)
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise ValueError("Zoom box must be x0 y0 x1 y1 with x1 > x0 >= 0, y1 > y0 >= 0")
    if size is not None and (x1 > size[0] or y1 > size[1]):
        raise ValueError(
            f"Zoom box ends outside the {size[0]}x{size[1]} screen; x1 and y1 are exclusive"
        )
    width, height = x1 - x0, y1 - y0
    if scale is None:
        scale = max(
            1, min(ZOOM_MAX_SCALE, math.floor(ZOOM_TARGET / max(width, height)))
        )
    if not 1 <= scale <= ZOOM_MAX_SCALE:
        raise ValueError(f"Zoom scale must be 1 to {ZOOM_MAX_SCALE}")
    if max(width, height) * scale > ZOOM_MAX_EDGE:
        raise ValueError(
            f"Zoom output would be {width * scale}x{height * scale}; keep its long "
            f"edge within {ZOOM_MAX_EDGE} px with a smaller box or --scale"
        )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    magick(
        [
            "import",
            "-window",
            "root",
            "-crop",
            f"{width}x{height}+{x0}+{y0}",
            "+repage",
            # Resizing promotes to 16-bit; keep ordinary 8-bit PNGs.
            "-depth",
            "8",
            *(
                ["-filter", "Lanczos", "-resize", f"{scale * 100}%"]
                if scale > 1
                else []
            ),
            str(output),
        ],
        env,
        label="magick zoom",
    )
    return str(output)
