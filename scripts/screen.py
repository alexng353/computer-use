"""Pixel-level helpers for the REPL: frame digests, waiting for a still screen, zoom."""

import hashlib
import math
import subprocess
import time
from pathlib import Path

# Crops smaller than this on their long edge are upscaled for reading.
ZOOM_TARGET = 800
ZOOM_MAX_SCALE = 8


def frame_digest(env):
    raw = subprocess.run(
        ["magick", "import", "-window", "root", "-depth", "8", "rgb:-"],
        env=env,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    return hashlib.sha256(raw).digest()


def wait_stable(env, timeout=5.0, quiet=0.3, interval=0.05):
    """Poll frames until none changes for `quiet` seconds; False at the cap."""
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


def zoom(env, box, output, scale=None):
    """Crop a fresh full-resolution capture to box (x0, y0, x1, y1, exclusive)."""
    x0, y0, x1, y1 = (int(value) for value in box)
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise ValueError("Zoom box must be x0 y0 x1 y1 with x1 > x0 >= 0, y1 > y0 >= 0")
    width, height = x1 - x0, y1 - y0
    if scale is None:
        scale = max(
            1, min(ZOOM_MAX_SCALE, math.floor(ZOOM_TARGET / max(width, height)))
        )
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
