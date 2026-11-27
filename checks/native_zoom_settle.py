"""Zoom reads 9 px text the full screenshot cannot; --settle waits out a delayed change.

Requires setup-ocr and tkinter. The fixture's editor-style status bar uses a dim
9 px font on a 2560x1600 desktop. RapidOCR reads that at native resolution, but
a model does not see native resolution: image inputs are downscaled to about a
1568 px long edge. The check OCRs the full shot as the model receives it, and
the zoom crop as returned. The Load button changes the page label two seconds
after each click.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from native_ocr import STATE, command, wait_until

STATUS = "Ln 128, Col 47  UTF-8  LF  4821 ms"
MODEL_LONG_EDGE = 1568


def fixture(directory):
    import tkinter as tk

    root = tk.Tk()
    root.title("Zoom settle fixture")
    root.geometry("1000x700+0+0")
    root.configure(bg="#1e2226")
    page = tk.Label(
        root, text="Waiting", font=("DejaVu Sans", 30), bg="#1e2226", fg="white"
    )
    page.pack(pady=60)
    clicks = 0

    def load():
        nonlocal clicks
        clicks += 1
        number = clicks
        root.after(2000, lambda: page.config(text=f"Loaded page {number}"))

    # Light-on-dark: RapidOCR misses dark text on a light Tk button at 2560 px.
    tk.Button(
        root,
        text="Load",
        font=("DejaVu Sans", 26),
        command=load,
        bg="#3a4148",
        fg="white",
        activebackground="#3a4148",
        activeforeground="white",
        relief="flat",
    ).pack(ipadx=40, ipady=10)
    status = tk.Label(
        root, text=STATUS, font=("DejaVu Sans", -9), bg="#2b3036", fg="#9aa1a8"
    )
    status.pack(side="bottom", anchor="w", padx=4, pady=2)

    def publish():
        root.update_idletasks()
        x, y = status.winfo_rootx(), status.winfo_rooty()
        bounds = [x, y, x + status.winfo_width(), y + status.winfo_height()]
        (directory / "status.json").write_text(json.dumps(bounds))

    root.after(800, publish)
    root.mainloop()


def normal(text):
    # OCR drops some word gaps ("Col47"), so compare without whitespace.
    return re.sub(r"\s+", "", text).lower()


def ocr_texts(name, image):
    cell = f"print(json.dumps([b['text'] for b in ocr(image={str(image)!r})]))"
    result = command("repl", name, "--code", "import json\n" + cell, timeout=120)
    return [normal(text) for text in json.loads(result.stdout)]


def last_path(stdout):
    return Path(stdout.strip().splitlines()[-1].removeprefix("[image] "))


def load_ref(name, directory):
    command(
        "screenshot",
        name,
        "--targets",
        "text",
        "--output",
        str(directory / "targets.png"),
    )
    snapshot = json.loads((STATE / name / "ocr-snapshot.json").read_text())
    found = [t for t in snapshot["targets"] if normal(t["text"]) == "load"]
    assert len(found) == 1, [t["text"] for t in snapshot["targets"]]
    return "@" + found[0]["ref"]


def open_fixture(name, size, directory):
    """Start a session showing the fixture; return the status label bounds."""
    directory.mkdir()
    command(
        "start",
        name,
        "--size",
        size,
        "--screenshot-directory",
        str(directory / "shots"),
    )
    command(
        "launch",
        name,
        "--id",
        "fixture",
        "--",
        sys.executable,
        __file__,
        "--fixture",
        str(directory),
    )
    wait_until(lambda: (directory / "status.json").exists(), "fixture geometry")
    window = next(
        w
        for w in json.loads(command("windows", name).stdout)
        if w["title"] == "Zoom settle fixture"
    )
    command("input", name, "--", "windowfocus", str(window["id"]))
    return json.loads((directory / "status.json").read_text())


def check_zoom(name, directory, keep):
    x0, y0, x1, y1 = open_fixture(name, "2560x1600", directory)
    full = directory / "full.png"
    command("screenshot", name, "--raw", "--output", str(full))
    assert normal(STATUS) in ocr_texts(name, full), "fixture text not rendered"
    seen = directory / "full-as-model-sees-it.png"
    subprocess.run(
        [
            "magick",
            str(full),
            "-resize",
            f"{MODEL_LONG_EDGE}x{MODEL_LONG_EDGE}>",
            str(seen),
        ],
        check=True,
    )
    full_texts = ocr_texts(name, seen)
    assert normal(STATUS) not in full_texts, ("full shot reads it", full_texts)
    zoomed = Path(
        command(
            "zoom",
            name,
            # OCR's detector needs some context around text that fills a crop.
            *map(str, (max(0, x0 - 12), y0 - 10, x1 + 12, min(1600, y1 + 10))),
            "--output",
            str(directory / "status-zoom.png"),
        ).stdout.strip()
    )
    assert zoomed == directory / "status-zoom.png" and zoomed.is_file()
    width = int(
        subprocess.run(
            ["magick", "identify", "-format", "%w", str(zoomed)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert width >= 2 * (x1 - x0), (width, x1 - x0)
    if keep:
        subprocess.run(["cp", str(zoomed), str(full), str(seen), keep], check=True)
    assert normal(STATUS) in ocr_texts(name, zoomed), ocr_texts(name, zoomed)
    rejected = command("zoom", name, "0", "0", "3000", "10", ok=False)
    assert "outside" in rejected.stderr, rejected.stderr
    print("PASS zoom reads 9 px status text that the full shot misses", flush=True)


def check_settle(name, directory):
    open_fixture(name, "1000x700", directory)
    # Control: without --settle the automatic capture precedes the change.
    plain = command("click", name, load_ref(name, directory))
    assert "loadedpage1" not in ocr_texts(name, last_path(plain.stdout))
    time.sleep(2.5)
    started = time.monotonic()
    settled = command(
        "click", name, load_ref(name, directory), "--settle=2500", timeout=60
    )
    elapsed = time.monotonic() - started
    assert "loadedpage2" in ocr_texts(name, last_path(settled.stdout)), settled.stdout
    assert elapsed < 10, elapsed
    print("PASS click --settle returns the screen after a 2 s change", flush=True)

    time.sleep(2.5)
    cell = "click(find(screenshot(targets='text'), text='Load'))"
    repl = command("repl", name, "--settle=2500", "--code", cell, timeout=120)
    assert "loadedpage3" in ocr_texts(name, last_path(repl.stdout)), repl.stdout
    waited = command("repl", name, "--code", "print(wait_stable(timeout=2, quiet=0.2))")
    assert waited.stdout.strip() == "True", waited.stdout
    command("input", name, "--settle", "--", "mousemove", "5", "5")
    print("PASS repl and input settle, and wait_stable shares the code", flush=True)


def main(directory, keep=None):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    tag = secrets.token_hex(3)
    # RapidOCR misses large, sparse text on a mostly empty 2560x1600 frame, so
    # settling (which needs the Load button's OCR) runs on a normal-size screen.
    names = {"zoom": f"check-zoom-{tag}", "settle": f"check-settle-{tag}"}
    try:
        check_zoom(names["zoom"], directory / "zoom", keep)
        check_settle(names["settle"], directory / "settle")
    finally:
        for name in names.values():
            if (STATE / name).exists():
                command("stop", name)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture"]:
        fixture(Path(sys.argv[2]))
        raise SystemExit(0)
    with tempfile.TemporaryDirectory(prefix="computer-use-zoom-check-") as temporary:
        main(Path(temporary), keep=sys.argv[2] if sys.argv[1:2] == ["--keep"] else None)
