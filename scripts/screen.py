"""Pixel helpers shared by the CLI (zoom, --settle) and the REPL (zoom, wait_stable)."""

import hashlib
import math
import subprocess
import time
from pathlib import Path

# Crops smaller than this on their long edge are upscaled for reading.
ZOOM_TARGET = 800
ZOOM_MAX_SCALE = 8
# Settling waits for this much unchanged screen, up to the cap.
SETTLE_QUIET = 0.5
SETTLE_CAP = 5.0


def frame_digest(env):
    raw = subprocess.run(
        ["magick", "import", "-window", "root", "-depth", "8", "rgb:-"],
        env=env,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    return hashlib.sha256(raw).digest()


def wait_stable(env, timeout=SETTLE_CAP, quiet=SETTLE_QUIET, interval=0.05):
    """Poll frames until none changes for `quiet` seconds; False at the cap.

    A change that starts later than `quiet` after the action is missed, so
    delayed loads need a quiet window longer than their delay.
    """
    # A cap shorter than the quiet window could never report a settled screen.
    timeout = max(timeout, 2 * quiet)
    deadline = time.monotonic() + timeout
    last = frame_digest(env)
    since = time.monotonic()
    while True:
        now = time.monotonic()
        if now - since >= quiet:
            return True
        if now >= deadline:
            return False
        time.sleep(interval)
        current = frame_digest(env)
        if current != last:
            last, since = current, time.monotonic()


def settle(env, milliseconds):
    """The CLI's --settle=MS: wait for MS of unchanged screen, capped."""
    return wait_stable(env, quiet=milliseconds / 1000)


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
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "magick",
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
        env=env,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return str(output)
