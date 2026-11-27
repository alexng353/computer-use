"""Zoom reads 9 px text the full screenshot cannot; --settle waits for a still screen.

Requires setup-ocr and tkinter. The fixture's editor-style status bar uses a dim
9 px font on a 2560x1600 desktop. RapidOCR reads that at native resolution, but
a model does not see native resolution: image inputs are downscaled to about a
1568 px long edge. The check OCRs the zoom crop as returned and, as a
precondition, the full shot as the model receives it.

The fixture's buttons: Load changes the page label two seconds after a click;
Animate changes it every 300 ms for 3 s, then shows "Animation done"; Spin
changes it every 200 ms for 6 s, then shows "Spin done".
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from native_ocr import CLI, STATE, command, wait_until

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
    page.pack(pady=40)
    clicks = 0

    def load():
        nonlocal clicks
        clicks += 1
        number = clicks
        root.after(2000, lambda: page.config(text=f"Loaded page {number}"))

    def frames(prefix, every, count, final):
        def step(index=1):
            if index > count:
                page.config(text=final)
                return
            page.config(text=f"{prefix} {index}")
            root.after(every, step, index + 1)

        return step

    for text, action in (
        ("Load", load),
        ("Animate", frames("Frame", 300, 10, "Animation done")),
        ("Spin", frames("Spin", 200, 30, "Spin done")),
    ):
        # Light-on-dark: RapidOCR misses dark text on a light Tk button at 2560 px.
        tk.Button(
            root,
            text=text,
            font=("DejaVu Sans", 26),
            command=action,
            bg="#3a4148",
            fg="white",
            activebackground="#3a4148",
            activeforeground="white",
            relief="flat",
        ).pack(ipadx=40, ipady=6, pady=8)
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


def cli(*args, env=None, timeout=90):
    """Run the CLI without asserting its exit status."""
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def target(name, directory, text):
    command(
        "screenshot",
        name,
        "--targets",
        "text",
        "--output",
        str(directory / "targets.png"),
    )
    snapshot = json.loads((STATE / name / "ocr-snapshot.json").read_text())
    found = [t for t in snapshot["targets"] if normal(t["text"]) == normal(text)]
    assert len(found) == 1, (text, [t["text"] for t in snapshot["targets"]])
    return found[0]


def ref(name, directory, text):
    return "@" + target(name, directory, text)["ref"]


def wait_still(name):
    command("repl", name, "--code", "wait_stable(timeout=15, quiet=1)", timeout=60)


def fake_magick(directory):
    """A magick that fails frame digests and settled captures, nothing else."""
    bin_dir = directory / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "magick"
    script.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case "$a" in rgb:-|*settled-*) '
        'echo "fake magick refused" >&2; exit 42;; esac; done\n'
        f'exec {shutil.which("magick")} "$@"\n'
    )
    script.chmod(0o700)
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")


def open_fixture(name, size, directory, shots=True):
    """Start a session showing the fixture; return the status label bounds."""
    directory.mkdir()
    command(
        "start",
        name,
        "--size",
        size,
        *(["--screenshot-directory", str(directory / "shots")] if shots else []),
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
    x0, y0, x1, y1 = open_fixture(name, "2560x1600", directory, shots=False)
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
    # A precondition about RapidOCR, not computer-use: a better OCR model may
    # read the downscale, which is no regression here.
    if normal(STATUS) in ocr_texts(name, seen):
        print("NOTE OCR reads the status in the downscaled shot; skipping that control")
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
    huge = command("zoom", name, "0", "0", "2560", "1600", "--scale", "8", ok=False)
    assert "8192" in huge.stderr, huge.stderr
    broken = command(
        "zoom", name, "0", "0", "10", "10", "--output", str(directory), ok=False
    )
    assert "magick" in broken.stderr and len(broken.stderr) > 60, broken.stderr
    print("PASS zoom reads 9 px status text that the full shot misses", flush=True)

    # repl --settle without a screenshot directory returns a raw settled image,
    # on its own line even after output without a newline.
    plain = command(
        "repl", name, "--settle=300", "--code", "print('payload', end='')", timeout=60
    )
    lines = plain.stdout.splitlines()
    assert lines[0] == "payload" and lines[1].startswith("[image] "), plain.stdout
    assert last_path(plain.stdout).is_file()
    failed = cli("repl", name, "--settle=300", "--code", "1 / 0")
    assert failed.returncode == 1 and "ZeroDivisionError" in failed.stderr, failed
    assert last_path(failed.stdout).is_file(), "a failed cell still settles"
    env = fake_magick(directory)
    broken = cli("repl", name, "--settle=300", "--code", "print('ran')", env=env)
    assert broken.returncode == 0 and broken.stdout == "ran\n", broken
    assert "completed" in broken.stderr and "repeat" in broken.stderr, broken.stderr
    print("PASS repl --settle: raw image on its own line; failures keep the status")


def check_settle(name, directory):
    open_fixture(name, "1000x700", directory)
    for value in ("0", "-5", "30001"):
        rejected = cli("input", name, f"--settle={value}", "--", "mousemove", "1", "1")
        assert rejected.returncode == 2 and "30000" in rejected.stderr, rejected
    print("PASS --settle accepts only 1 to 30000 ms", flush=True)

    # Control: without --settle the automatic capture precedes the change.
    plain = command("click", name, ref(name, directory, "Load"))
    assert "loadedpage1" not in ocr_texts(name, last_path(plain.stdout))
    wait_still(name)
    settled = command(
        "click", name, ref(name, directory, "Load"), "--settle=2500", timeout=60
    )
    assert "loadedpage2" in ocr_texts(name, last_path(settled.stdout)), settled.stdout
    print("PASS click --settle returns the screen after a 2 s change", flush=True)

    # A screen that keeps changing for 3 s must be waited out, not slept past.
    started = time.monotonic()
    animated = command(
        "click", name, ref(name, directory, "Animate"), "--settle=1000", timeout=60
    )
    elapsed = time.monotonic() - started
    assert "animationdone" in ocr_texts(name, last_path(animated.stdout)), animated
    assert "still changing" not in animated.stderr and elapsed >= 3.5, elapsed
    print("PASS --settle waits out a screen that changes for 3 s", flush=True)

    x, y = target(name, directory, "Load")["center"]
    moved = command(
        "input",
        name,
        "--settle=2500",
        "--",
        "mousemove",
        str(x),
        str(y),
        "click",
        "1",
        timeout=60,
    )
    assert "loadedpage3" in ocr_texts(name, last_path(moved.stdout)), moved.stdout
    print("PASS input --settle returns the delayed redraw", flush=True)

    started = time.monotonic()
    spinning = command(
        "click", name, ref(name, directory, "Spin"), "--settle=500", timeout=60
    )
    elapsed = time.monotonic() - started
    assert "still changing" in spinning.stderr, spinning.stderr
    assert 4.5 <= elapsed < 10, elapsed
    wait_still(name)
    print("PASS a screen that never settles warns at the cap", flush=True)

    cell = (
        "import time\n"
        "t = time.monotonic()\n"
        "still = wait_stable(timeout=3, quiet=0.8)\n"
        "print(still, time.monotonic() - t >= 0.8)\n"
        "click(find(screenshot(targets='text'), text='Spin'))\n"
        "print(wait_stable(timeout=1.5, quiet=0.5))\n"
        "try:\n"
        "    wait_stable(timeout=0.2, quiet=1)\n"
        "except ValueError:\n"
        "    print('refused')\n"
        "print(wait_stable(timeout=15, quiet=1))\n"
        "click(find(screenshot(targets='text'), text='Load'))\n"
        "wait_stable(timeout=6, quiet=2.5)\n"
        "display(screenshot(targets=None))\n"
    )
    waited = command("repl", name, "--code", cell, timeout=120)
    expected = ["True True", "False", "refused", "True"]
    assert waited.stdout.splitlines()[:4] == expected, waited.stdout
    assert "loadedpage4" in ocr_texts(name, last_path(waited.stdout)), waited.stdout
    print("PASS REPL wait_stable waits, gives up while spinning, sees a redraw")

    # A failing settle after the action ran warns and keeps the exit code.
    env = fake_magick(directory)
    broken = cli("exec", name, "--settle=300", "--", "sh", "-c", "exit 3", env=env)
    assert broken.returncode == 3, broken
    assert "completed" in broken.stderr and "repeat" in broken.stderr, broken.stderr
    assert last_path(broken.stdout).is_file(), broken.stdout
    print("PASS a failed settle warns, captures and keeps the exit code", flush=True)

    # Stop cancels a long settle instead of waiting for it.
    long_settle = ["input", name, "--settle=20000", "--", "mousemove", "3", "3"]
    waiting = subprocess.Popen(
        [sys.executable, str(CLI), *long_settle],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)
    started = time.monotonic()
    command("stop", name, timeout=60)
    stopped = time.monotonic() - started
    _, stderr = waiting.communicate(timeout=30)
    assert stopped < 5, stopped
    assert waiting.returncode != 0 and "stopping" in stderr, stderr
    print("PASS stop cancels a long settle", flush=True)


def main(directory, keep=None):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    tag = secrets.token_hex(3)
    # RapidOCR misses large, sparse text on a mostly empty 2560x1600 frame, so
    # settling (which needs the buttons' OCR) runs on a normal-size screen.
    names = {"zoom": f"check-zoom-{tag}", "settle": f"check-settle-{tag}"}
    try:
        check_zoom(names["zoom"], directory / "zoom", keep)
        check_settle(names["settle"], directory / "settle")
    finally:
        for name in names.values():
            if (STATE / name).exists():
                command("stop", name, timeout=90)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture"]:
        fixture(Path(sys.argv[2]))
        raise SystemExit(0)
    with tempfile.TemporaryDirectory(prefix="computer-use-zoom-check-") as temporary:
        main(Path(temporary), keep=sys.argv[2] if sys.argv[1:2] == ["--keep"] else None)
