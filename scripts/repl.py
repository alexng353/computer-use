"""Persistent Python REPL that runs inside one session's display environment.

The CLI side (`call`) starts the server as a session-owned service and sends one
cell per request over a private Unix socket. The server keeps one namespace for
the session's lifetime and preloads helpers that reuse the CLI's own code paths:
reference verification before clicks, the a-g snapshot sequence, invalidation
after input, and the session-owned clipboard.

While a cell runs the server is authoritative for state.json, and the cell
never outlives the session's interaction lock: the CLI passes its locked file
to the server, so the lock stays held until both the CLI and the cell are done,
even if the CLI is killed. The server interrupts a cell whose client went away.
"""

import ast
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
MAX_MESSAGE = 16 * 1024 * 1024
# Cell output beyond this is cut so the reply stays under MAX_MESSAGE.
MAX_OUTPUT = 8 * 1024 * 1024
# Time an interrupted cell gets to finish before its REPL is stopped.
GRACE = 5.0
# The server serves one connection at a time, so a request must arrive quickly.
REQUEST_TIMEOUT = 2.0
PING_TIMEOUT = 5.0


def socket_path(state):
    return Path(state["directory"]) / "repl.sock"


def unit_name(state):
    return state["prefix"] + "-repl.service"


def notice(message):
    print("computer-use: " + message, file=sys.stderr, flush=True)


def exchange(state, message, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path(state)))
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as response:
            return json.loads(response.readline(MAX_MESSAGE))


def fresh(state):
    """Reload state.json; the REPL server may have written it during a cell."""
    import computer_use

    current = computer_use.load(state["name"])
    if current["prefix"] != state["prefix"]:
        raise RuntimeError("Session was replaced while this command ran; retry")
    return current


def discard(state):
    """Stop the REPL, then drop only its unit from the state on disk."""
    import computer_use

    unit = unit_name(state)
    subprocess.run(
        ["systemctl", "--user", "stop", unit], capture_output=True, check=False
    )
    if computer_use.active(unit):
        raise RuntimeError(f"Could not stop {unit}")
    # Reload only now: the cell can record units while it is being stopped.
    current = fresh(state)
    if unit in current["units"]:
        current["units"].remove(unit)
        computer_use.save(current)
    socket_path(current).unlink(missing_ok=True)
    return current


def status(state):
    """ready, busy (accepts but does not answer) or dead."""
    try:
        reply = exchange(state, {"action": "ping"}, timeout=PING_TIMEOUT)
    except TimeoutError:
        return "busy"
    except (OSError, ValueError):
        return "dead"
    return "ready" if reply.get("ready") else "dead"


def ensure(state, start_service, reset=False):
    """Return fresh state with a ready REPL; never replace a live one silently."""
    import computer_use

    state = fresh(state)
    unit = unit_name(state)
    recorded = unit in state["units"]
    if reset:
        lost = "--reset" if recorded else None
    else:
        current = status(state)
        if current == "ready":
            return state
        if current == "busy":
            raise RuntimeError(
                "The REPL is not answering; a cell from an earlier call may still be "
                "running. Retry, or pass --reset to stop it and discard its state"
            )
        lost = (
            None
            if not recorded
            else "it stopped answering"
            if computer_use.active(unit)
            else "its service had stopped"
        )
    if len(os.fsencode(socket_path(state))) >= 108:
        raise RuntimeError("Session path is too long for the REPL socket")
    state = discard(state)
    start_service(
        state,
        unit,
        [sys.executable, str(SCRIPTS / "repl.py"), "--session", state["name"]],
    )
    deadline = time.monotonic() + 20
    while status(state) != "ready":
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"REPL did not start; inspect journalctl --user -u {unit}"
            )
        time.sleep(0.05)
    if lost:
        notice(f"started a fresh REPL; previous state lost ({lost})")
    return state


def interrupt(state):
    # SIGINT raises KeyboardInterrupt inside the cell and keeps the namespace.
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


def call(state, request, start_service, lock, stopping, timeout, reset=False):
    """Run one cell while holding `lock`; return the server's reply.

    A timeout, `stopping()` or SIGTERM/SIGINT/SIGHUP interrupts the cell and
    waits for it to end before returning, so the lock is never released while
    the cell can still drive the desktop.
    """
    payload = json.dumps({"action": "run", **request}, ensure_ascii=False).encode()
    payload += b"\n"
    if len(payload) > MAX_MESSAGE:
        raise RuntimeError("Cell request too large (over 16 MiB)")
    state = ensure(state, start_service, reset=reset)
    signals = []
    previous = {
        number: signal.signal(number, lambda signum, _frame: signals.append(signum))
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    }
    reason = None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(PING_TIMEOUT)
            connection.connect(str(socket_path(state)))
            # The server keeps this open file description until the cell ends.
            socket.send_fds(connection, [b"L"], [lock.fileno()])
            connection.sendall(payload)
            connection.settimeout(0.1)
            received = bytearray()
            deadline = time.monotonic() + timeout
            interrupted_at = None
            while not received.endswith(b"\n"):
                now = time.monotonic()
                if reason is None:
                    if signals:
                        reason = (
                            f"this command received {signal.Signals(signals[0]).name}"
                        )
                    elif stopping():
                        reason = "the session is stopping"
                    elif now >= deadline:
                        reason = f"its {timeout:g} s timeout passed"
                    if reason is not None:
                        interrupt(state)
                        interrupted_at = now
                elif now >= interrupted_at + GRACE:
                    discard(state)
                    raise RuntimeError(
                        f"Cell ignored its interrupt ({reason}); the REPL was "
                        "stopped and its state lost"
                    )
                try:
                    chunk = connection.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    raise RuntimeError(
                        "REPL exited during the cell; inspect journalctl --user -u "
                        + unit_name(state)
                    )
                received += chunk
                if len(received) > MAX_MESSAGE:
                    raise RuntimeError("Cell output exceeded 16 MiB")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    reply = json.loads(received)
    if reply.get("interrupted"):
        reply["error"] = (
            f"Cell interrupted because {reason or 'of an interrupt'}; REPL state is kept\n"
            + (reply.get("error") or "")
        )
    elif reason is not None and not reason.startswith("its "):
        # The cell finished before the interrupt landed, but the caller asked to stop.
        reply["error"] = f"Cell finished, but {reason}\n" + (reply.get("error") or "")
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
        # Relative paths resolve against the calling command's directory.
        output = Path(output).resolve() if output else self.next_image(targets or "raw")
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

        output = Path(output).resolve() if output else self.next_image("zoom")
        return screen.zoom(
            self.env, box, output, scale=scale, size=self.cu.screen_size(self.state)
        )

    def wait_stable(self, timeout=None, quiet=None):
        """Wait until the screen stops changing; returns True if it settled."""
        import screen

        return screen.wait_stable(
            self.env,
            timeout=screen.SETTLE_CAP if timeout is None else timeout,
            quiet=screen.SETTLE_QUIET if quiet is None else quiet,
        )

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
            # The OCR worker has its own working directory; send absolute paths.
            source = Path(image).resolve() if image else Path(temporary) / "source.png"
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

    def spawn(self, *command, capture=False):
        """Start a process in this display; it ends with the REPL or the session.

        Output goes to a log file (its path is `.log`) so a chatty app never
        blocks on a full pipe. capture=True gives text pipes for communicate().
        """
        self.invalidate()
        if capture:
            process = subprocess.Popen(
                command,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            process.log = None
            return process
        logs = Path(self.state["directory"]) / "spawn-logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        self.counter += 1
        log = logs / f"{self.counter:04d}-{Path(command[0]).name}.log"
        with log.open("ab") as output:
            process = subprocess.Popen(
                command,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        process.log = str(log)
        return process

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
        return CDP(self, ports[0]) if ports else None


class CDP:
    """The session browser's DevTools port, driven through agent-browser.

    `run` may change the page, so it retires screenshot references first.
    """

    binary = "agent-browser"

    def __init__(self, session, port):
        self.session = session
        self.port = port
        self.url = f"http://127.0.0.1:{port}"

    def json(self, path="version"):
        with urllib.request.urlopen(f"{self.url}/json/{path}", timeout=5) as response:
            return json.loads(response.read())

    def run(self, *args, timeout=60):
        self.session.invalidate()
        return subprocess.run(
            [
                self.binary,
                "--session",
                self.session.name,
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

RUNNING = threading.Event()


def interrupt_cell(_signum, _frame):
    # Signals outside a cell (a late timeout, a stray kill) are ignored.
    if RUNNING.is_set():
        raise KeyboardInterrupt


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


@contextlib.contextmanager
def captured_output(path, collected):
    """Send fds 1 and 2, and sys.stdout/stderr, to one file for the cell.

    Subprocesses that inherit the descriptors land in the reply in order. The
    file is kept: a child that outlives its cell keeps writing there, not into
    the reply.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    streams = sys.stdout, sys.stderr
    saved = os.dup(1), os.dup(2)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("w+b") as capture:
        os.dup2(capture.fileno(), 1)
        os.dup2(capture.fileno(), 2)
        sys.stdout = open(1, "w", buffering=1, errors="replace", closefd=False)
        sys.stderr = open(2, "w", buffering=1, errors="replace", closefd=False)
        try:
            yield
        finally:
            for stream in (sys.stdout, sys.stderr):
                with contextlib.suppress(OSError, ValueError):
                    stream.flush()
            sys.stdout, sys.stderr = streams
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
            capture.seek(0)
            output = capture.read(MAX_OUTPUT + 1)
            collected.append(
                fit_output(output[:MAX_OUTPUT].decode(errors="replace"), output)
            )


def fit_output(text, raw):
    """Cut text so its JSON encoding stays within MAX_OUTPUT bytes."""
    marker = "\n[output truncated to fit the reply]\n"
    size = len(json.dumps(text, ensure_ascii=False).encode())
    if size <= MAX_OUTPUT and len(raw) <= MAX_OUTPUT:
        return text
    keep = len(text)
    while size > MAX_OUTPUT - len(marker) * 6:
        # Escaping can grow a character up to six bytes; shrink until it fits.
        keep = int(keep * (MAX_OUTPUT - len(marker) * 6) / size * 0.95)
        size = len(json.dumps(text[:keep], ensure_ascii=False).encode())
    return text[:keep] + marker


def watch_client(connection, done):
    """Interrupt the cell if its client disappears; end the REPL if it won't stop."""
    while not done.wait(0.1):
        try:
            if connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                break
        except BlockingIOError:
            continue
        except OSError:
            break
    else:
        return
    os.kill(os.getpid(), signal.SIGINT)
    if not done.wait(GRACE):
        # systemd then stops what the cell started; the lock closes with us.
        os._exit(70)


def read_request(connection):
    """Return (message, lock fd or None) from one bounded request."""
    connection.settimeout(REQUEST_TIMEOUT)
    data, fds, _flags, _address = socket.recv_fds(connection, 1, 2)
    for fd in fds:
        # A child the cell starts (`cmd &`) must never inherit the session lock.
        os.set_inheritable(fd, False)
    for extra in fds[1:]:
        os.close(extra)
    lock = fds[0] if fds else None
    try:
        buffer = bytearray(b"" if data == b"L" else data)
        too_large = False
        while not buffer.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                raise ValueError("incomplete request")
            if too_large or len(buffer) + len(chunk) > MAX_MESSAGE:
                # Read the rest so the sender can still receive the refusal.
                too_large = True
                buffer = bytearray(chunk[-1:])
            else:
                buffer += chunk
        if too_large:
            raise ValueError("request too large")
        message = json.loads(buffer)
        check_request(message)
        return message, lock
    except BaseException:
        if lock is not None:
            os.close(lock)
        raise


def check_request(message):
    if not isinstance(message, dict):
        raise ValueError("request must be a JSON object")
    if message.get("action") == "run":
        if not isinstance(message.get("code"), str):
            raise ValueError("code must be a string")
        for field in ("image_dir", "cwd"):
            if not isinstance(message.get(field), (str, type(None))):
                raise ValueError(f"{field} must be a string or null")


def run_cell(session, namespace, message, connection):
    try:
        session.refresh(message.get("image_dir"))
    except (OSError, ValueError, RuntimeError) as exc:
        return {"error": f"Bad request: {exc}"}
    with contextlib.suppress(OSError, TypeError):
        os.chdir(message.get("cwd"))
    namespace["cdp"] = session.cdp
    # A cell may have replaced the handler; every cell starts interruptible.
    signal.signal(signal.SIGINT, interrupt_cell)
    connection.settimeout(None)
    done = threading.Event()
    threading.Thread(target=watch_client, args=(connection, done), daemon=True).start()
    collected, error, interrupted = [], None, False
    session.counter += 1
    output = (
        Path(session.state["directory"])
        / "spawn-logs"
        / f"cell-{session.counter:04d}.out"
    )
    try:
        with captured_output(output, collected):
            RUNNING.set()
            try:
                execute(message["code"], namespace)
            finally:
                RUNNING.clear()
    except KeyboardInterrupt as exc:
        interrupted = True
        error = "".join(traceback.format_exception(exc))
    except SystemExit as exc:
        if exc.code:
            error = "".join(traceback.format_exception(exc))
    except BaseException as exc:  # noqa: BLE001 - The cell's failure is the reply, never the server's.
        error = "".join(traceback.format_exception(exc))
    finally:
        done.set()
    return {
        "stdout": collected[0] if collected else "",
        "images": session.images,
        "error": error,
        "interrupted": interrupted,
    }


def serve(name):
    sys.path.insert(0, str(SCRIPTS))
    session = Session(name)
    namespace = {"__name__": "__repl__", "session": session}
    namespace.update({helper: getattr(session, helper) for helper in HELPERS})
    signal.signal(signal.SIGINT, interrupt_cell)
    path = socket_path(session.state)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(4)
        while True:
            connection, _ = server.accept()
            lock = None
            with connection:
                try:
                    message, lock = read_request(connection)
                except (OSError, ValueError) as error:
                    reply = {"error": f"Bad request: {error}"}
                else:
                    if message.get("action") == "ping":
                        reply = {"ready": True}
                    elif message.get("action") == "run" and isinstance(
                        message.get("code"), str
                    ):
                        reply = run_cell(session, namespace, message, connection)
                    else:
                        reply = {"error": "Bad request: unknown action"}
                try:
                    connection.sendall(
                        json.dumps(reply, ensure_ascii=False).encode() + b"\n"
                    )
                except OSError:
                    pass
                finally:
                    # Releasing our share of the lock lets the next command run.
                    if lock is not None:
                        os.close(lock)


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:2] != ["--session"] or len(sys.argv) != 3:
        raise SystemExit("usage: repl.py --session NAME")
    serve(sys.argv[2])
