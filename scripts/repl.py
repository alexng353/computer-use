"""Persistent Python REPL that runs inside one session's display environment.

The CLI side (`call`) starts the server as a session-owned service and sends one
cell per request over a private Unix socket. The server keeps one namespace for
the session's lifetime and preloads helpers that reuse the CLI's own code paths:
reference verification before clicks, the a-g snapshot sequence, invalidation
after input, and the session-owned clipboard.
"""

import ast
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
MAX_MESSAGE = 16 * 1024 * 1024


def socket_path(state):
    return Path(state["directory"]) / "repl.sock"


def unit_name(state):
    return state["prefix"] + "-repl.service"


def exchange(state, message, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path(state)))
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as response:
            return json.loads(response.readline(MAX_MESSAGE))


def active(unit):
    return (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit], check=False
        ).returncode
        == 0
    )


def discard(state, save_state):
    unit = unit_name(state)
    if unit in state["units"]:
        subprocess.run(
            ["systemctl", "--user", "stop", unit], capture_output=True, check=False
        )
        if active(unit):
            raise RuntimeError(f"Could not stop {unit}")
        state["units"].remove(unit)
        save_state(state)
    socket_path(state).unlink(missing_ok=True)


def ensure(state, start_service, save_state):
    if len(os.fsencode(socket_path(state))) >= 108:
        raise RuntimeError("Session path is too long for the REPL socket")
    try:
        if exchange(state, {"action": "ping"}, timeout=1).get("ready"):
            return
    except (OSError, ValueError):
        pass
    # A crashed server leaves its unit recorded; replace it without duplicating it.
    discard(state, save_state)
    start_service(
        state,
        unit_name(state),
        [sys.executable, str(SCRIPTS / "repl.py"), "--session", state["name"]],
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if exchange(state, {"action": "ping"}, timeout=1).get("ready"):
                return
        except (OSError, ValueError):
            time.sleep(0.05)
    raise RuntimeError(
        f"REPL did not start; inspect journalctl --user -u {unit_name(state)}"
    )


def call(state, request, start_service, save_state, stopping, timeout):
    """Run one cell; return the server's reply. `stopping()` aborts the wait."""
    ensure(state, start_service, save_state)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        connection.connect(str(socket_path(state)))
        connection.sendall(json.dumps({"action": "run", **request}).encode() + b"\n")
        connection.settimeout(0.1)
        received = bytearray()
        deadline = time.monotonic() + timeout
        interrupted = None
        while not received.endswith(b"\n"):
            if stopping():
                raise RuntimeError("Cell cancelled because the session is stopping")
            now = time.monotonic()
            if interrupted is None and now >= deadline:
                # SIGINT raises KeyboardInterrupt inside the cell and keeps state.
                subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "kill",
                        "--signal=SIGINT",
                        "--kill-whom=main",
                        unit_name(state),
                    ],
                    capture_output=True,
                    check=False,
                )
                interrupted = now
            elif interrupted is not None and now >= interrupted + 5:
                discard(state, save_state)
                raise RuntimeError(
                    "Cell ignored its interrupt; the REPL was restarted and its state lost"
                )
            try:
                chunk = connection.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                raise RuntimeError(
                    f"REPL exited during the cell; inspect journalctl --user -u {unit_name(state)}"
                )
            received += chunk
            if len(received) > MAX_MESSAGE:
                raise RuntimeError("Cell output exceeded 16 MiB")
    reply = json.loads(received)
    if interrupted is not None:
        reply["error"] = (
            f"Cell interrupted after its {timeout:g} s timeout; state is kept\n"
            + (reply.get("error") or "")
        )
    return reply


# ---- Server side: everything below runs inside the session's service. ----


class Session:
    """Helpers bound to one desktop. Each cell reloads the session's state."""

    def __init__(self, name):
        import computer_use
        import ocr_targets

        self.cu = computer_use
        self.targets = ocr_targets
        self.name = name
        self.state = computer_use.load(name)
        self.images = []
        self.image_dir = Path(self.state["directory"]) / "repl-images"
        self.counter = 0

    # Plumbing

    def refresh(self, image_dir=None):
        current = self.cu.load(self.name)
        if current["prefix"] != self.state["prefix"]:
            raise RuntimeError("Session was replaced; this REPL belongs to the old one")
        self.state = current
        self.images = []
        self.image_dir = (
            Path(image_dir) if image_dir else Path(current["directory"]) / "repl-images"
        )

    @property
    def env(self):
        return dict(os.environ, **self.cu.environment(self.state))

    def run(self, *args, timeout=30):
        return subprocess.run(
            args,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def invalidate(self):
        self.targets.invalidate(self.state)

    def xdotool(self, *args):
        """Raw xdotool in this display; retires current references like `input`."""
        self.invalidate()
        return self.run("xdotool", *map(str, args))

    def next_image(self, label):
        self.image_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.counter += 1
        return self.image_dir / f"{os.getpid()}-{self.counter:04d}-{label}.png"

    def capture(self, output):
        self.cu.capture(self.env, output)

    def display(self, item):
        """Return an image (path, screenshot or zoom result) with this cell."""
        path = item.get("image") if isinstance(item, dict) else item
        if path is None or not Path(path).is_file():
            raise ValueError("display() needs an image path or a screenshot result")
        self.images.append(str(Path(path).resolve()))

    # Observation

    def screenshot(self, targets="accessibility", output=None):
        """Capture with targets (accessibility, text, icons) or None for raw.

        Returns the snapshot: image path, size and targets with refs, like `query`.
        """
        output = Path(output) if output else self.next_image(targets or "raw")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.targets.screenshot(
            self.state,
            output,
            self.capture,
            self.cu.service,
            raw=targets is None,
            target_mode=targets or "accessibility",
        )
        if targets is None:
            return {"image": str(output), "targets": []}
        snapshot = Path(self.state["directory"]) / "ocr-snapshot.json"
        return json.loads(snapshot.read_text())

    def find(self, snapshot=None, text=None, role=None, exact=True):
        """Return the one target in a snapshot matching text and/or role."""
        snapshot = snapshot or self.screenshot()

        def matches(target):
            name = target.get("text") or ""
            if text is not None and (
                name != text if exact else text.lower() not in name.lower()
            ):
                return False
            return role is None or target.get("role") == role

        found = [t for t in snapshot["targets"] if matches(t)]
        if len(found) != 1:
            raise LookupError(
                f"{len(found)} targets match text={text!r} role={role!r}: "
                + ", ".join(f"@{t['ref']} {t.get('text')!r}" for t in found[:10])
            )
        return found[0]

    def zoom(self, box, scale=None, output=None):
        """Crop a fresh capture to box=(x0, y0, x1, y1); returns the image path."""
        import screen

        output = Path(output) if output else self.next_image("zoom")
        return screen.zoom(self.env, box, output, scale=scale)

    def wait_stable(self, timeout=5.0, quiet=0.3):
        """Wait until the screen stops changing; returns True if it settled."""
        import screen

        return screen.wait_stable(self.env, timeout=timeout, quiet=quiet)

    def a11y(self, max_nodes=3000):
        """Accessibility tree of every app in this session, as indented text."""
        import accessibility_targets

        if not self.state.get("accessibility_bus"):
            raise RuntimeError("This session has no accessibility bus")
        return accessibility_targets.tree_text(self.env, self.state, max_nodes)

    def ocr(self, image=None):
        """OCR text boxes of a fresh capture (or an image path); refs are untouched."""
        self.targets.ensure_worker(self.state, self.cu.service, target_mode="text")
        with tempfile.TemporaryDirectory(dir=self.state["directory"]) as temporary:
            source = Path(image) if image else Path(temporary) / "source.png"
            if not image:
                self.capture(source)
            result = self.targets.request(
                self.state,
                {
                    "action": "recognize",
                    "source": str(source),
                    "output": str(Path(temporary) / "annotated.png"),
                    "prefix": "a",
                },
            )
        return [
            {key: t[key] for key in ("text", "confidence", "bounds", "center")}
            for t in result["targets"]
        ]

    def windows(self):
        return self.cu.list_windows(self.env)

    def wait_window(self, title, timeout=10.0):
        """Wait for a visible window whose title contains `title`."""
        deadline = time.monotonic() + timeout
        while True:
            found = [w for w in self.windows() if title in w["title"]]
            if found:
                return found[0]
            if time.monotonic() >= deadline:
                raise TimeoutError(f"No window titled {title!r} after {timeout:g} s")
            time.sleep(0.1)

    # Input

    def focus(self, window):
        window_id = window["id"] if isinstance(window, dict) else window
        self.xdotool("windowfocus", "--sync", window_id)

    def click(self, target, y=None, button=1, double=False):
        """Click a reference ("@a3" or a target dict) after verification, or x, y."""
        clicks = ["--repeat", "2"] if double else []

        def native(x, y):
            self.run(
                "xdotool",
                "mousemove",
                "--sync",
                str(x),
                str(y),
                "click",
                "--clearmodifiers",
                *clicks,
                str(button),
            )

        if y is not None:
            self.invalidate()
            native(int(target), int(y))
            return
        reference = target["ref"] if isinstance(target, dict) else str(target)
        self.targets.click(self.state, reference, self.capture, native, self.cu.service)

    def type(self, text, paste=None):
        """Type text; multiline or non-ASCII text goes through the session clipboard."""
        if paste is None:
            paste = not (text.isascii() and text.isprintable())
        if paste:
            self.clipboard(text)
            self.key("ctrl+v")
        else:
            self.xdotool("type", "--clearmodifiers", "--delay", "1", "--", text)

    def key(self, *combos):
        self.xdotool("key", "--clearmodifiers", *combos)

    def scroll(self, dy=1, dx=0, x=None, y=None):
        """Wheel clicks: positive dy scrolls down, positive dx right."""
        if x is not None:
            self.xdotool("mousemove", "--sync", x, y)
        for amount, negative, positive in ((dy, 4, 5), (dx, 6, 7)):
            if amount:
                button = positive if amount > 0 else negative
                self.xdotool("click", "--repeat", abs(int(amount)), button)

    def drag(self, x0, y0, x1, y1, steps=12, button=1):
        self.xdotool("mousemove", "--sync", x0, y0, "mousedown", button)
        try:
            for step in range(1, steps + 1):
                self.xdotool(
                    "mousemove",
                    "--sync",
                    round(x0 + (x1 - x0) * step / steps),
                    round(y0 + (y1 - y0) * step / steps),
                )
        finally:
            self.xdotool("mouseup", button)

    def clipboard(self, text=None):
        """Set the session clipboard to text, or return it when text is None."""
        if text is None:
            return self.cu.get_clipboard(self.env)
        self.invalidate()
        self.cu.set_clipboard(self.state, text, self.env)
        return None

    # Processes

    def spawn(self, *command):
        """Start a process in this display; it ends with the REPL or the session."""
        self.invalidate()
        return subprocess.Popen(
            command,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def launch(self, app_id, *command):
        """Start a session-owned app service, like `computer-use launch`."""
        self.invalidate()
        return self.cu.launch(self.state, app_id, list(command))

    @property
    def cdp(self):
        ports = [
            app["cdp_port"]
            for app in self.state["apps"].values()
            if app.get("cdp_port")
        ]
        return CDP(self.name, ports[0]) if ports else None


class CDP:
    """The session browser's DevTools port, driven through agent-browser."""

    def __init__(self, session, port):
        self.session = session
        self.port = port
        self.url = f"http://127.0.0.1:{port}"

    def json(self, path="version"):
        with urllib.request.urlopen(f"{self.url}/json/{path}", timeout=5) as response:
            return json.loads(response.read())

    def run(self, *args, timeout=60):
        return subprocess.run(
            [
                "agent-browser",
                "--session",
                self.session,
                "--cdp",
                str(self.port),
                *map(str, args),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def __repr__(self):
        return f"CDP(port={self.port})"


HELPERS = (
    "click",
    "type",
    "key",
    "scroll",
    "drag",
    "screenshot",
    "find",
    "zoom",
    "a11y",
    "ocr",
    "wait_stable",
    "windows",
    "wait_window",
    "focus",
    "clipboard",
    "spawn",
    "launch",
    "display",
    "xdotool",
)


def execute(code, namespace):
    """Run a cell; echo the last expression's value like an interactive prompt."""
    tree = ast.parse(code, "<cell>", "exec")
    tail = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tail = ast.Expression(tree.body.pop().value)
    exec(compile(tree, "<cell>", "exec"), namespace)
    if tail is not None:
        value = eval(compile(tail, "<cell>", "eval"), namespace)
        if value is not None:
            print(repr(value))


def serve(name):
    sys.path.insert(0, str(SCRIPTS))
    session = Session(name)
    namespace = {"__name__": "__repl__", "session": session}
    namespace.update({helper: getattr(session, helper) for helper in HELPERS})
    running = False

    def interrupt(_signum, _frame):
        if running:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    path = socket_path(session.state)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(4)
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    with connection.makefile("rb") as incoming:
                        message = json.loads(incoming.readline(MAX_MESSAGE))
                except (OSError, ValueError):
                    continue
                if message.get("action") == "ping":
                    reply = {"ready": True}
                else:
                    stdout, error = io.StringIO(), None
                    try:
                        session.refresh(message.get("image_dir"))
                        running = True
                        with (
                            contextlib.redirect_stdout(stdout),
                            contextlib.redirect_stderr(stdout),
                        ):
                            execute(message["code"], namespace)
                    except BaseException as exc:  # noqa: BLE001 - The cell's failure is the reply, never the server's.
                        if isinstance(exc, SystemExit) and not exc.code:
                            error = None
                        else:
                            error = "".join(traceback.format_exception(exc))
                    finally:
                        running = False
                    reply = {
                        "stdout": stdout.getvalue(),
                        "images": session.images,
                        "error": error,
                    }
                try:
                    connection.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:2] != ["--session"] or len(sys.argv) != 3:
        raise SystemExit("usage: repl.py --session NAME")
    serve(sys.argv[2])
