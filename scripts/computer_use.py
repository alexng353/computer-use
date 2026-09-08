#!/usr/bin/env python3
"""Run and control X11 desktop apps on a private virtual display."""

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import ocr_targets

ROOT = Path.home() / ".local/state/computer-use"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def require(*commands):
    for command in commands:
        if not shutil.which(command):
            raise RuntimeError(f"Missing dependency: {command}")


def identifier(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", value):
        raise argparse.ArgumentTypeError(
            "Use 1-41 lowercase letters, digits or hyphens"
        )
    return value


def poll(check, message, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise RuntimeError(message)


def active(unit):
    return (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit], check=False
        ).returncode
        == 0
    )


def save(state):
    target = Path(state["directory"]) / "state.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(target)


def load(name):
    target = ROOT / name / "state.json"
    if not target.is_file():
        raise RuntimeError(f"Session {name!r} not found; run computer-use start {name}")
    return json.loads(target.read_text())


@contextmanager
def interaction_lock(name):
    # A name's lock must survive stop/recreate while old commands are waiting.
    locks = ROOT / ".locks"
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (locks / (name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def reload_session(expected):
    current = load(expected["name"])
    if current["prefix"] != expected["prefix"]:
        raise RuntimeError(
            "Session was replaced while this command waited; retry explicitly"
        )
    return current


def stop_marker(state):
    return Path(state["directory"]) / (state["prefix"] + ".stop")


def run_interruptible(state, command, env):
    def interrupted(_signum, _frame):
        raise RuntimeError("Command interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with subprocess.Popen(command, env=env, start_new_session=True) as process:
            try:
                while True:
                    if stop_marker(state).exists():
                        raise RuntimeError(
                            "Command cancelled because the session is stopping"
                        )
                    try:
                        return process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
    finally:
        signal.signal(signal.SIGTERM, previous)


def environment(state):
    return {
        "DISPLAY": state["display"],
        "XAUTHORITY": state["xauthority"],
        "WAYLAND_DISPLAY": "",
        "XDG_SESSION_TYPE": "x11",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + state["bus_socket"],
        "GTK_USE_PORTAL": "0",
        "GDK_BACKEND": "x11",
        "QT_QPA_PLATFORM": "xcb",
        "SDL_VIDEODRIVER": "x11",
        "CLUTTER_BACKEND": "x11",
        "MOZ_ENABLE_WAYLAND": "0",
        "XDG_ACTIVATION_TOKEN": "",
        "DESKTOP_STARTUP_ID": "",
        "AT_SPI_BUS_ADDRESS": "",
    }


def check_session(state):
    for unit in [state["display_unit"], state["bus_unit"]]:
        if not active(unit):
            raise RuntimeError(f"{unit} is inactive; stop and recreate this session")


def service(state, unit, command, isolated=True):
    # Record ownership before starting, so a failed launch can still be cleaned up.
    state["units"].append(unit)
    save(state)
    env_args = (
        [f"--setenv={key}={value}" for key, value in environment(state).items()]
        if isolated
        else []
    )
    run(
        "systemd-run",
        "--user",
        "--collect",
        "--unit=" + unit,
        "--working-directory=" + str(Path.cwd()),
        *env_args,
        "--",
        *command,
    )


def stop(state, remove=True):
    for unit in reversed(state["units"]):
        subprocess.run(
            ["systemctl", "--user", "stop", unit], capture_output=True, check=False
        )
        if active(unit):
            raise RuntimeError(f"Could not stop {unit}; retaining session files")
    if remove:
        shutil.rmtree(state["directory"])


def start(name, size):
    require("Xvfb", "xauth", "xdpyinfo", "systemd-run", "xdg-dbus-proxy")
    if not re.fullmatch(r"[1-9][0-9]{2,3}x[1-9][0-9]{2,3}", size):
        raise RuntimeError("--size must be WIDTHxHEIGHT, from 100 to 9999 pixels")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = ROOT / name
    root.mkdir(mode=0o700)  # Refuse to overwrite even an incomplete session.
    prefix = f"computer-use-{name}-{secrets.token_hex(3)}"
    state = {
        "name": name,
        "directory": str(root),
        "size": size,
        "display_unit": prefix + "-display.service",
        "bus_unit": prefix + "-bus.service",
        "prefix": prefix,
        "bus_socket": str(root / "bus"),
        "xauthority": str(root / "Xauthority"),
        "units": [],
        "apps": {},
    }
    save(state)
    try:
        number = next(
            (
                n
                for n in range(90, 190)
                if not Path(f"/tmp/.X{n}-lock").exists()
                and not Path(f"/tmp/.X11-unix/X{n}").exists()
            ),
            None,
        )
        if number is None:
            raise RuntimeError("No free virtual display number")
        state["display"] = f":{number}"
        authority = Path(state["xauthority"])
        authority.touch(mode=0o600)
        run(
            "xauth",
            "-f",
            str(authority),
            input=f"add {state['display']} . {secrets.token_hex(16)}\n",
        )
        service(
            state,
            state["display_unit"],
            [
                "Xvfb",
                state["display"],
                "-screen",
                "0",
                size + "x24",
                "-nolisten",
                "tcp",
                "-auth",
                str(authority),
            ],
            isolated=False,
        )
        env = dict(os.environ, **environment(state))
        poll(
            lambda: (
                subprocess.run(
                    ["xdpyinfo"], env=env, capture_output=True, timeout=2, check=False
                ).returncode
                == 0
            ),
            "Xvfb did not become ready",
        )
        host_bus = os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
        )
        # Shared portals can launch native dialogs on the host despite DISPLAY.
        service(
            state,
            state["bus_unit"],
            [
                "xdg-dbus-proxy",
                host_bus,
                state["bus_socket"],
                "--filter",
                "--talk=org.freedesktop.secrets",
                "--talk=org.kde.kwalletd5",
                "--talk=org.kde.kwalletd6",
            ],
            isolated=False,
        )
        poll(
            lambda: Path(state["bus_socket"]).exists(),
            "Filtered bus did not become ready",
        )
        check_session(state)
        return state
    except Exception:
        stop(state)
        raise


def clone_auth(source, destination, profile):
    source_profile = source / profile
    if not source_profile.is_dir():
        raise RuntimeError(f"No source profile: {source_profile}")
    destination.mkdir(mode=0o700)
    dest_profile = destination / profile
    dest_profile.mkdir(mode=0o700)
    if (source / "Local State").is_file():
        shutil.copy2(source / "Local State", destination / "Local State")
    for name in ["Preferences", "Secure Preferences"]:
        if (source_profile / name).is_file():
            shutil.copy2(source_profile / name, dest_profile / name)
    preferences = dest_profile / "Preferences"
    if preferences.is_file():
        prefs = json.loads(preferences.read_text())
        prefs.setdefault("profile", {}).update(exit_type="Normal", exited_cleanly=True)
        preferences.write_text(json.dumps(prefs))
    for name in ["Cookies", "Network/Cookies"]:
        src = source_profile / name
        if src.is_file():
            dst = dest_profile / name
            dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (
                sqlite3.connect(src.as_uri() + "?mode=ro", uri=True) as inp,
                sqlite3.connect(dst) as out,
            ):
                inp.backup(out)


def launch(state, app_id, command):
    if app_id in state["apps"]:
        raise RuntimeError(f"App id {app_id!r} already used in this session")
    require(command[0])
    unit = state["prefix"] + "-app-" + app_id + ".service"
    state["apps"][app_id] = {"unit": unit}
    service(state, unit, command)
    return state["apps"][app_id]


def browser(state, args):
    require("helium-browser")
    if not args.profile or "/" in args.profile or args.profile in [".", ".."]:
        raise RuntimeError("--profile must name one browser profile directory")
    if args.id in state["apps"]:
        raise RuntimeError(f"App id {args.id!r} already used in this session")
    profile = Path(state["directory"]) / (args.id + "-profile")
    if args.source_profile:
        clone_auth(args.source_profile.expanduser().resolve(), profile, args.profile)
    else:
        profile.mkdir(mode=0o700)
    app = None
    try:
        app = launch(
            state,
            args.id,
            [
                "helium-browser",
                "--ozone-platform=x11",
                "--user-data-dir=" + str(profile),
                "--profile-directory=" + args.profile,
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--disable-sync",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=" + state["size"].replace("x", ","),
                args.url,
            ],
        )

        def ready():
            try:
                port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as res:
                    return port if res.status == 200 else None
            except (OSError, ValueError, IndexError):
                return None

        app["cdp_port"] = poll(
            ready, "Helium did not expose CDP; inspect its systemd journal"
        )
        app["profile"] = str(profile)
        save(state)
        return app
    except Exception:
        if app is not None:
            subprocess.run(
                ["systemctl", "--user", "stop", app["unit"]],
                capture_output=True,
                check=False,
            )
        # Keep failed app files for diagnosis; stop removes the whole session.
        raise


def status(state):
    return {
        **state,
        "display_active": active(state["display_unit"]),
        "bus_active": active(state["bus_unit"]),
        "apps": {
            key: {**app, "active": active(app["unit"])}
            for key, app in state["apps"].items()
        },
    }


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List owned sessions")
    commands.add_parser("setup-ocr", help="Install and warm the local CPU OCR runtime")
    for action in [
        "start",
        "status",
        "stop",
        "launch",
        "browser",
        "exec",
        "input",
        "screenshot",
        "click",
        "query",
        "windows",
        "clipboard",
    ]:
        sub = commands.add_parser(action)
        sub.add_argument("name", type=identifier)
        if action == "start":
            sub.add_argument("--size", default="1440x1000")
        elif action in ["launch", "browser"]:
            sub.add_argument(
                "--id",
                type=identifier,
                default="app" if action == "launch" else "browser",
            )
            if action == "browser":
                sub.add_argument("--source-profile", type=Path)
                sub.add_argument("--profile", default="Default")
                sub.add_argument("--url", default="about:blank")
        elif action == "screenshot":
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument(
                "--raw", action="store_true", help="Capture without OCR targets"
            )
        elif action in ["click", "query"]:
            sub.add_argument(
                "reference", help="Current screenshot reference, e.g. @a13"
            )
        elif action == "clipboard":
            sub.add_argument("operation", choices=["get", "set"])
    return cli


def main():
    os.umask(0o077)
    argv = sys.argv[1:]
    command = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    cli = parser()
    args = cli.parse_args(argv)
    if (args.action in ["launch", "exec", "input"]) != bool(command):
        cli.error(
            "launch, exec and input require a command after --; other actions do not"
        )
    if args.action == "setup-ocr":
        require("uv")
        ocr_targets.setup()
        print("Local CPU OCR runtime ready")
        return
    if args.action == "list":
        print(
            json.dumps(
                [
                    status(load(p.parent.name))
                    for p in sorted(ROOT.glob("*/state.json"))
                ],
                indent=2,
            )
        )
        return
    if args.action == "start":
        with interaction_lock(args.name):
            print(json.dumps(start(args.name, args.size), indent=2))
        return
    state = load(args.name)
    if args.action == "status":
        print(json.dumps(status(state), indent=2))
        return
    if args.action == "stop":
        # Signal an unbounded input/exec before waiting for its interaction lock.
        stop_marker(state).touch()
    with interaction_lock(args.name):
        state = reload_session(state)
        if args.action == "stop":
            stop(state)
            print("Stopped session and removed its temporary files and login profiles")
            return
        if stop_marker(state).exists():
            raise RuntimeError("Session is stopping")
        check_session(state)
        perform(state, args, command)


def perform(state, args, command):
    env = dict(os.environ, **environment(state))

    def capture(output):
        require("magick")
        run("magick", "import", "-window", "root", str(output), env=env)

    if args.action in ["launch", "browser", "exec", "input"] or (
        args.action == "clipboard" and args.operation == "set"
    ):
        ocr_targets.invalidate(state)
    if args.action == "launch":
        print(json.dumps(launch(state, args.id, command), indent=2))
    elif args.action == "browser":
        print(json.dumps(browser(state, args), indent=2))
    elif args.action in ["exec", "input"]:
        if args.action == "input":
            command = ["xdotool", *command]
        raise SystemExit(run_interruptible(state, command, env))
    elif args.action == "screenshot":
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        ocr_targets.screenshot(state, output, capture, service, raw=args.raw)
        print(output)
    elif args.action == "query":
        snapshot, target = ocr_targets.query(state, args.reference)
        print(
            json.dumps(
                {
                    **target,
                    "image": snapshot["image"],
                    "size": snapshot["size"],
                    "coordinate_origin": "top-left",
                    "bounds_format": "x1,y1,x2,y2 (exclusive end)",
                },
                indent=2,
            )
        )
    elif args.action == "click":
        require("xdotool")

        def native_click(x, y):
            run(
                "xdotool",
                "mousemove",
                "--sync",
                str(x),
                str(y),
                "click",
                "--clearmodifiers",
                "1",
                env=env,
            )

        ocr_targets.click(state, args.reference, capture, native_click, service)
    elif args.action == "windows":
        require("xdotool")
        result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "."],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode not in [0, 1]:
            raise RuntimeError(result.stderr)
        windows = []
        for window in result.stdout.splitlines():
            title = subprocess.run(
                ["xdotool", "getwindowname", window],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            if title.returncode == 0:
                windows.append({"id": int(window), "title": title.stdout.rstrip("\n")})
        print(json.dumps(windows, indent=2))
    elif args.action == "clipboard":
        require("xclip")
        if args.operation == "get":
            sys.stdout.write(
                run(
                    "xclip", "-selection", "clipboard", "-out", env=env, timeout=5
                ).stdout
            )
        else:
            clipboard = Path(state["directory"]) / "clipboard.txt"
            clipboard.write_text(sys.stdin.read())
            unit = state["prefix"] + "-clipboard.service"
            if unit in state["units"]:
                run("systemctl", "--user", "stop", unit)
                state["units"].remove(unit)
            service(
                state,
                unit,
                ["xclip", "-selection", "clipboard", "-in", "-quiet", str(clipboard)],
            )
            poll(
                lambda: (
                    subprocess.run(
                        ["xclip", "-selection", "clipboard", "-out"],
                        env=env,
                        capture_output=True,
                        timeout=1,
                        check=False,
                    ).returncode
                    == 0
                ),
                "Clipboard owner did not become ready",
                seconds=5,
            )


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        sqlite3.Error,
    ) as exc:
        print(f"computer-use: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
