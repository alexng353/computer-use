"""Exercise real OCR, native clicks, cancellation and session reuse on Xvfb.

Requires setup-ocr, the usual desktop dependencies, and Python's tkinter module.
Uses only uniquely named test desktops and an optional artifact directory.
"""

import argparse
import fcntl
import importlib.util
import json
import os
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/computer_use.py"
STATE = Path.home() / ".local/state/computer-use"


def fixture(directory):
    import tkinter as tk

    root = tk.Tk()
    root.title("OCR target verification")
    root.geometry("1000x700+0+0")
    root.configure(bg="#202428")
    label = tk.Label(
        root,
        text="Native click verification",
        font=("DejaVu Sans", 28),
        bg="#202428",
        fg="white",
    )
    label.pack(pady=80)
    count = 0

    def save():
        nonlocal count
        count += 1
        label.config(text=f"Saved {count}")
        (directory / "clicked.json").write_text(
            json.dumps(
                {
                    "saved": count,
                    "pointer": [root.winfo_pointerx(), root.winfo_pointery()],
                }
            )
        )

    button = tk.Button(root, text="Save", font=("DejaVu Sans", 24), command=save)
    button.pack(pady=30, ipadx=50, ipady=15)
    tk.Label(
        root,
        text="Another Save label",
        font=("DejaVu Sans", 18),
        bg="#202428",
        fg="white",
    ).pack(pady=30)

    def update():
        command = directory / "change-label"
        if command.exists():
            button.config(text=command.read_text())
            root.update_idletasks()
            command.unlink()
        root.after(20, update)

    update()
    root.mainloop()


def wait_until(predicate, description):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out: {description}")


def command(*args, ok=True, timeout=30):
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert (result.returncode == 0) == ok, (args, result.stdout, result.stderr)
    return result


def main(directory):
    os.umask(0o077)
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-ocr-" + secrets.token_hex(4)
    session_dir = STATE / name
    running = []
    child_handles = []
    sys.path.insert(0, str(CLI.parent))
    spec = importlib.util.spec_from_file_location("computer_use", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def snapshot():
        return json.loads((session_dir / "ocr-snapshot.json").read_text())

    def capture():
        command("screenshot", name, "--output", str(directory / "targets.png"))
        return snapshot()

    def read_target(data, text):
        return next(item for item in data["targets"] if item["text"] == text)

    def pending_input(*args):
        process = subprocess.Popen(
            [sys.executable, str(CLI), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        running.append(process)
        return process

    def lock_busy():
        with (STATE / ".locks" / (name + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False

    try:
        command("start", name, "--size", "1000x700")
        command(
            "launch",
            name,
            "--id",
            "fixture",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "--fixture",
            str(directory),
        )
        wait_until(
            lambda: json.loads(command("windows", name).stdout), "fixture window"
        )
        command("screenshot", name, "--raw", "--output", str(directory / "raw.png"))
        first = capture()
        shutil.copyfile(directory / "raw.png", directory / "initial-raw.png")
        shutil.copyfile(directory / "targets.png", directory / "initial-targets.png")
        target = read_target(first, "Save")
        assert first["prefix"] == "a"
        query = json.loads(command("query", name, "@" + target["ref"]).stdout)
        assert query["center"] == target["center"]
        assert query["screen_size"] == query["size"] == [1000, 700]
        assert query["image_size"] == [1000, 700]
        command("click", name, "@" + target["ref"])
        wait_until(
            lambda: (directory / "clicked.json").exists(), "native click callback"
        )
        clicked = json.loads((directory / "clicked.json").read_text())
        assert clicked == {"saved": 1, "pointer": target["center"]}, clicked
        command("query", name, "@" + target["ref"], ok=False)
        second = capture()
        assert second["prefix"] == "b"
        command("query", name, "@" + target["ref"], ok=False)
        # The pointer is already at this center; the second click must not hang.
        command("click", name, "@" + read_target(second, "Save")["ref"], timeout=5)
        wait_until(
            lambda: json.loads((directory / "clicked.json").read_text())["saved"] == 2,
            "second click callback",
        )
        print(
            "PASS native query/click, repeated center and old/post-click refs",
            flush=True,
        )

        target = read_target(capture(), "Save")
        change = directory / "change-label"
        change.write_text("Changed")
        wait_until(lambda: not change.exists(), "background text redraw")
        rejected = command("click", name, "@" + target["ref"], ok=False)
        assert "pixels changed" in rejected.stderr
        assert not (session_dir / "ocr-snapshot.json").exists()
        prefixes = [capture()["prefix"] for _ in range(8)]
        assert prefixes[0] == prefixes[7] and len(set(prefixes[:7])) == 7, prefixes
        old = snapshot()["targets"][0]["ref"]
        command("screenshot", name, "--raw", "--output", str(directory / "raw.png"))
        command("query", name, "@" + old, ok=False)
        old = capture()["targets"][0]["ref"]
        command("input", name, "--", "mousemove", "20", "20")
        command("query", name, "@" + old, ok=False)
        print("PASS redraw rejection, prefix wrap, raw/input invalidation", flush=True)

        state = module.load(name)
        unit = state["prefix"] + "-ocr.service"
        subprocess.run(["systemctl", "--user", "stop", unit], check=True)
        capture()
        assert module.load(name)["units"].count(unit) == 1
        (session_dir / "ocr-generation.json").write_text("{")
        failed = command(
            "screenshot", name, "--output", str(directory / "bad.png"), ok=False
        )
        assert "Traceback" not in failed.stderr
        command("stop", name)
        assert not session_dir.exists() and not module.active(unit)
        print("PASS worker restart, malformed metadata error and cleanup", flush=True)

        for args in (
            ("input", name, "--", "search", "--sync", "--name", "__never__"),
            ("exec", name, "--", sys.executable, "-c", "import signal; signal.pause()"),
        ):
            command("start", name, "--size", "800x600")
            process = pending_input(*args)
            wait_until(lock_busy, "pending command lock")
            command("stop", name, timeout=5)
            _, error = process.communicate(timeout=5)
            assert process.returncode != 0 and "cancelled" in error, error
            assert not session_dir.exists()
        print("PASS stop cancels blocked input and exec", flush=True)

        command("start", name, "--size", "800x600")
        child_ready = directory / "child.pid"
        child_code = (
            "import os, signal, sys\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "signal.pause()\n"
        )
        parent_code = (
            "import signal, subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}, sys.argv[1]])\n"
            "signal.pause()\n"
        )
        process = pending_input(
            "exec", name, "--", sys.executable, "-c", parent_code, str(child_ready)
        )
        wait_until(
            lambda: child_ready.exists() and child_ready.read_text().isdigit(),
            "TERM-resistant descendant",
        )
        child_handle = os.pidfd_open(int(child_ready.read_text()))
        child_handles.append(child_handle)
        command("stop", name, timeout=5)
        assert select.select([child_handle], [], [], 2)[0], "Descendant survived stop"
        _, error = process.communicate(timeout=5)
        assert process.returncode != 0 and "cancelled" in error, error
        print(
            "PASS stop kills TERM-resistant descendant after leader exits", flush=True
        )

        command("start", name, "--size", "800x600")
        old_state = module.load(name)
        # Hold the name lock while deliberately replacing its session. An old
        # queued stop must retain its original identity after acquiring the lock.
        with module.interaction_lock(name):
            process = pending_input("stop", name)
            wait_until(lambda: module.stop_marker(old_state).exists(), "queued stop")
            module.stop(old_state)
            replacement = module.start(name, "800x600")
        _, error = process.communicate(timeout=5)
        assert process.returncode != 0 and "replaced" in error, error
        assert module.load(name)["prefix"] == replacement["prefix"]
        assert module.active(replacement["display_unit"])
        assert capture()["targets"] == []
        command("query", name, "@a1", ok=False)
        command("stop", name)
        print("PASS queued stop rejects replacement session; empty screen", flush=True)
    finally:
        for child_handle in child_handles:
            try:
                signal.pidfd_send_signal(child_handle, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(child_handle)
        for process in running:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        if session_dir.exists():
            command("stop", name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.fixture:
        fixture(args.fixture)
    elif args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        main(args.output.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="computer-use-check-") as scratch:
            main(Path(scratch))
