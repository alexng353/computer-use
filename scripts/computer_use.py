#!/usr/bin/env python3
"""Run and control X11 desktop apps on a private virtual display."""

import argparse
import ctypes
import datetime
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
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import ocr_targets
import accessibility_targets
import repl
import screen

ROOT = Path.home() / ".local/state/computer-use"
# Pruned sessions move to <TRASH_PARENT>/tmp-trash-YYYYMMDD/computer-use-<name>.
TRASH_PARENT = Path.home() / ".local/state"
# The open interaction lock of this command, shared with a running REPL cell.
HELD_LOCK = None


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def require(*commands):
    for command in commands:
        if not shutil.which(command):
            raise RuntimeError(f"Missing dependency: {command}")


def configure_session_environment():
    # Agent shells can omit login variables while the user's manager is running.
    runtime = Path(f"/run/user/{os.getuid()}")
    bus = runtime / "bus"
    if (
        runtime.is_dir()
        and runtime.stat().st_uid == os.getuid()
        and bus.is_socket()
        and bus.stat().st_uid == os.getuid()
    ):
        os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime))
        if os.environ["XDG_RUNTIME_DIR"] == str(runtime):
            os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")


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
    global HELD_LOCK
    locks = ROOT / ".locks"
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (locks / (name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        HELD_LOCK = lock
        try:
            yield lock
        finally:
            HELD_LOCK = None


def reload_session(expected):
    current = load(expected["name"])
    if current["prefix"] != expected["prefix"]:
        raise RuntimeError(
            "Session was replaced while this command waited; retry explicitly"
        )
    return current


def unit_state(unit):
    # A unit that was never loaded or has been collected reports "inactive".
    return subprocess.run(
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", unit],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def loaded_session_units():
    listed = subprocess.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "computer-use-*",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.split()[0] for line in listed.splitlines() if line.strip()]


def rename_noreplace(source, destination):
    """rename(2) that fails with FileExistsError instead of replacing anything."""
    libc = ctypes.CDLL(None, use_errno=True)
    at_fdcwd, rename_noreplace_flag = -100, 1
    if libc.renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace_flag,
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def move_to_trash(directory, trash, name):
    trash.mkdir(mode=0o700, parents=True, exist_ok=True)
    suffix = 1
    while True:
        destination = trash / (
            f"computer-use-{name}" + (f"-{suffix}" if suffix > 1 else "")
        )
        # Claiming the slot and moving are one step, so a concurrent prune or
        # an existing entry (even a broken symlink) is never replaced or nested.
        try:
            rename_noreplace(directory, destination)
            return destination
        except FileExistsError:
            suffix += 1


def reset_failed(unit):
    subprocess.run(
        ["systemctl", "--user", "reset-failed", unit],
        capture_output=True,
        check=False,
    )


def recorded_units(state):
    return list(
        dict.fromkeys(
            unit
            for unit in [
                *state["units"],
                state.get("display_unit"),
                state.get("bus_unit"),
            ]
            if unit
        )
    )


def prune_session(directory, locks, trash, dry, owners):
    """Judge one session under its lock; return (outcome, detail)."""
    name = directory.name
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (locks / (name + ".lock")).open("a") as lock:
        # `start` saves state before its units run; its lock marks it busy.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "kept", "busy: a command holds its lock"
        # Read under the lock: a stop + start can replace the session up to here.
        try:
            state = json.loads((directory / "state.json").read_text())
            units = recorded_units(state)
            if "prefix" not in state:
                return "kept", "no readable state.json"
            if Path(state["directory"]).resolve() != directory.resolve():
                return "kept", "state.json names another directory"
        except FileNotFoundError:
            if not directory.exists():
                return "gone", None
            return "kept", "no readable state.json"
        except (OSError, ValueError, KeyError, TypeError):
            return "kept", "no readable state.json"
        owners[name] = set(units)
        live = [
            unit for unit in units if unit_state(unit) not in ("inactive", "failed")
        ]
        if live:
            return "kept", "active units: " + ", ".join(live)
        if dry:
            return "prune", None
        destination = move_to_trash(directory, trash, name)
        del owners[name]
        for unit in units:
            reset_failed(unit)
        return "pruned", {"trash": str(destination), "units": units}


def prune(dry=False, root=None, trash_parent=None, day=None, list_units=None):
    """Move sessions whose units are all inactive into dated trash; never delete."""
    root = root or ROOT
    trash = (trash_parent or TRASH_PARENT) / (
        "tmp-trash-" + (day or datetime.date.today()).strftime("%Y%m%d")
    )
    candidates, pruned, kept = [], [], []
    owners = {}  # Remaining sessions: name -> recorded units.
    directories = (
        sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        if root.is_dir()
        else []
    )
    for directory in directories:
        try:
            outcome, detail = prune_session(
                directory, root / ".locks", trash, dry, owners
            )
        except OSError as error:
            # One bad entry is reported; the rest of the run continues.
            outcome, detail = "kept", f"error: {error}"
        if outcome == "kept":
            kept.append({"name": directory.name, "reason": detail})
        elif outcome == "prune":
            candidates.append(directory.name)
        elif outcome == "pruned":
            pruned.append({"name": directory.name, **detail})
    result = {"kept": kept}
    try:
        listed = (list_units or loaded_session_units)()
    except (OSError, subprocess.SubprocessError) as error:
        listed = None
        result["orphan_error"] = str(error)
    orphans, reset = [], []
    for unit in listed or []:
        if any(unit in recorded for recorded in owners.values()):
            continue
        # Units are named computer-use-<folder>-...; this covers unrecorded units
        # of remaining sessions and sessions that started during this run.
        if any(
            unit.startswith(f"computer-use-{path.name}-")
            for path in (root.iterdir() if root.is_dir() else [])
            if not path.name.startswith(".")
        ):
            continue
        current = unit_state(unit)
        if current == "inactive":
            continue
        if current == "failed" and not dry:
            reset_failed(unit)
            reset.append(unit)
            continue
        orphans.append({"unit": unit, "state": current})
    result["orphan_units"] = None if listed is None else orphans
    if dry:
        return {"prune": candidates, **result}
    return {"pruned": pruned, **result, "reset_units": reset}


def stop_marker(state):
    return Path(state["directory"]) / (state["prefix"] + ".stop")


def run_interruptible(state, command, env):
    def interrupted(_signum, _frame):
        raise RuntimeError("Command interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with subprocess.Popen(command, env=env, start_new_session=True) as process:
            completed = False
            try:
                while True:
                    if stop_marker(state).exists():
                        raise RuntimeError(
                            "Command cancelled because the session is stopping"
                        )
                    try:
                        result = process.wait(timeout=0.1)
                        completed = True
                        return result
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if not completed:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    # The leader can exit before a descendant that ignores TERM.
                    # Keep the grace period and escalation tied to the group.
                    deadline = time.monotonic() + 2
                    while True:
                        process.poll()
                        try:
                            os.killpg(process.pid, 0)
                        except ProcessLookupError:
                            break
                        if time.monotonic() >= deadline:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            break
                        time.sleep(0.02)
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
        "AT_SPI_BUS_ADDRESS": state.get("accessibility_bus", ""),
        "ACCESSIBILITY_ENABLED": "1" if state.get("accessibility_bus") else "0",
    }


def check_session(state):
    for unit in [state["display_unit"], state["bus_unit"]]:
        if not active(unit):
            raise RuntimeError(f"{unit} is inactive; stop and recreate this session")


def capture(env, output):
    require("magick")
    run("magick", "import", "-window", "root", str(output), env=env)


def list_windows(env):
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
    return windows


def get_clipboard(env):
    require("xclip")
    return run("xclip", "-selection", "clipboard", "-out", env=env, timeout=5).stdout


def set_clipboard(state, text, env):
    # A session-owned xclip holds the selection inside this display only.
    require("xclip")
    clipboard = Path(state["directory"]) / "clipboard.txt"
    clipboard.write_text(text)
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


def start(name, size, accessibility=True, screenshot_directory=None):
    require("Xvfb", "xauth", "xdpyinfo", "systemd-run", "xdg-dbus-proxy")
    if screenshot_directory is not None:
        require("magick")
        screenshot_directory = screenshot_directory.expanduser().resolve()
        screenshot_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    if screenshot_directory is not None:
        state["screenshot_directory"] = str(screenshot_directory)
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
        if accessibility:
            accessibility_targets.enable(state, service, save)
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


def viewer(state):
    require("ffmpeg", "xdotool")
    directory = Path(state["directory"])
    info = directory / "viewer-info.json"
    if "viewer" not in state["apps"]:
        launch(
            state,
            "viewer",
            [
                sys.executable,
                str(Path(__file__).resolve().with_name("live_view.py")),
                str(directory),
            ],
        )
    app = state["apps"]["viewer"]

    def ready():
        if not active(app["unit"]):
            raise RuntimeError(
                "Viewer stopped; inspect its service log and recreate the session"
            )
        try:
            details = json.loads(info.read_text())
            with urllib.request.urlopen(
                details["url"] + "frame", timeout=2
            ) as response:
                if response.read(2) == b"\xff\xd8":
                    return {**app, **details}
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None
        return None

    return poll(ready, "Viewer did not produce a frame; inspect its service log")


def native_browser_command():
    # Distribution wrappers may inject debugging flags from user/system config.
    # Native mode invokes the packaged ELF binary with only the requested flags.
    wrapper = Path(shutil.which("helium-browser")).resolve()
    for binary in (wrapper, wrapper.with_name("helium")):
        if binary.is_file() and os.access(binary, os.X_OK):
            with binary.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    directory = wrapper.parent
                    library_path = ":".join(
                        str(directory / suffix) for suffix in (".", "lib", "lib.target")
                    )
                    if os.environ.get("LD_LIBRARY_PATH"):
                        library_path += ":" + os.environ["LD_LIBRARY_PATH"]
                    return [
                        "env",
                        "CHROME_WRAPPER=" + str(wrapper),
                        "LD_LIBRARY_PATH=" + library_path,
                        str(binary),
                    ]
    raise RuntimeError(
        "--no-cdp requires the packaged Helium ELF binary beside its launcher"
    )


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
                *(native_browser_command() if args.no_cdp else ["helium-browser"]),
                "--ozone-platform=x11",
                "--user-data-dir=" + str(profile),
                "--profile-directory=" + args.profile,
                *(
                    []
                    if args.no_cdp
                    else [
                        "--remote-debugging-address=127.0.0.1",
                        "--remote-debugging-port=0",
                    ]
                ),
                *(
                    ["--force-renderer-accessibility=complete"]
                    if state.get("accessibility_bus")
                    else []
                ),
                "--disable-sync",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=" + state["size"].replace("x", ","),
                args.url,
            ],
        )

        def ready():
            if args.no_cdp:
                pid = run(
                    "systemctl",
                    "--user",
                    "show",
                    "--value",
                    "--property=MainPID",
                    app["unit"],
                ).stdout.strip()
                return (
                    pid != "0"
                    and subprocess.run(
                        ["xdotool", "search", "--onlyvisible", "--pid", pid],
                        env=dict(os.environ, **environment(state)),
                        capture_output=True,
                        check=False,
                    ).returncode
                    == 0
                )
            try:
                port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as res:
                    return port if res.status == 200 else None
            except (OSError, ValueError, IndexError):
                return None

        port = poll(ready, "Helium did not become ready; inspect its systemd journal")
        app["cdp_port"] = None if args.no_cdp else port
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


def capture_image(state, output):
    capture(dict(os.environ, **environment(state)), output)


def screen_size(state):
    return tuple(int(value) for value in state["size"].split("x"))


def settle(state, args):
    """Wait out redraws after an action; a settled repl also returns its screen."""
    env = dict(os.environ, **environment(state))
    if not screen.settle(env, args.settle):
        print(
            "computer-use: screen still changing at the settle cap; capturing anyway",
            file=sys.stderr,
        )
    if args.action != "repl":
        return
    # The REPL server may have written state.json during the cell.
    state = repl.fresh(state)
    if state.get("screenshot_directory"):
        image = automatic_screenshot(state, "repl")
    else:
        directory = (
            args.output_dir.expanduser().resolve()
            if args.output_dir
            else Path(state["directory"]) / "repl-images"
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        image = directory / f"settled-{time.time_ns()}.png"
        capture(env, image)
    if image:
        print("[image] " + str(image))


def automatic_screenshot(state, action):
    directory = state.get("screenshot_directory")
    if directory is None or stop_marker(state).exists():
        return None
    destination = Path(directory)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(
        prefix=f"{state['name']}-{action}-", suffix=".png", dir=destination
    )
    os.close(descriptor)
    output = Path(filename)
    raw = not state.get("accessibility_bus") or not state["apps"]
    try:
        for attempt in range(3):
            if stop_marker(state).exists():
                output.unlink(missing_ok=True)
                return None
            try:
                ocr_targets.screenshot(
                    state,
                    output,
                    lambda path: capture_image(state, path),
                    service,
                    raw=raw,
                    target_mode="accessibility",
                )
                break
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                # Navigation can replace the focused window during annotation.
                # Retry the observation only; the interaction already happened.
                if "Focused window changed" in str(error) and attempt < 2:
                    continue
                if raw:
                    raise
                print(
                    f"computer-use: Automatic annotations unavailable ({error}); "
                    "returning a raw screenshot without references",
                    file=sys.stderr,
                )
                ocr_targets.screenshot(
                    state,
                    output,
                    lambda path: capture_image(state, path),
                    service,
                    raw=True,
                )
                break
        state["screenshot"] = str(output)
        save(state)
        return str(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise


def print_result(result, screenshot=None):
    if isinstance(result, dict):
        print(
            json.dumps(
                result | ({"screenshot": screenshot} if screenshot else {}), indent=2
            )
        )
    elif screenshot:
        print(("\n" if isinstance(result, int) else "") + screenshot, flush=True)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List owned sessions")
    prune_command = commands.add_parser(
        "prune",
        help="Move sessions whose units are all inactive into dated trash",
    )
    prune_command.add_argument(
        "--dry", action="store_true", help="Only list what would be moved"
    )
    commands.add_parser("setup-ocr", help="Install and warm the local CPU OCR runtime")
    commands.add_parser(
        "setup-icons", help="Install and warm optional local CPU icon detection"
    )
    commands.add_parser(
        "setup-accessibility", help="Check native accessibility dependencies"
    )
    for action in [
        "start",
        "status",
        "stop",
        "launch",
        "browser",
        "viewer",
        "exec",
        "input",
        "screenshot",
        "click",
        "query",
        "windows",
        "clipboard",
        "repl",
        "zoom",
    ]:
        sub = commands.add_parser(action)
        sub.add_argument("name", type=identifier)
        if action in ["input", "click", "exec", "repl"]:
            sub.add_argument(
                "--settle",
                nargs="?",
                const=int(screen.SETTLE_QUIET * 1000),
                type=int,
                metavar="MS",
                help="After the action, wait until the screen is unchanged for MS "
                f"milliseconds (default {int(screen.SETTLE_QUIET * 1000)}; capped at "
                f"{screen.SETTLE_CAP:g} s or twice MS) before any capture",
            )
        if action == "start":
            sub.add_argument("--size", default="1440x1000")
            sub.add_argument(
                "--screenshot-directory",
                type=Path,
                help="Save and return a new screenshot after every interaction",
            )
            sub.add_argument(
                "--accessibility",
                action=argparse.BooleanOptionalAction,
                default=True,
                help="Enable a private accessibility bus for this session (default: enabled)",
            )
        elif action in ["launch", "browser"]:
            sub.add_argument(
                "--accessibility",
                action="store_true",
                help="Enable accessibility for this and future app launches",
            )
            sub.add_argument(
                "--id",
                type=identifier,
                default="app" if action == "launch" else "browser",
            )
            if action == "browser":
                sub.add_argument("--source-profile", type=Path)
                sub.add_argument("--profile", default="Default")
                sub.add_argument("--url", default="about:blank")
                sub.add_argument(
                    "--no-cdp",
                    action="store_true",
                    help="Launch with native input and no debugging port",
                )
        elif action == "screenshot":
            sub.add_argument("--output", type=Path, required=True)
            capture_mode = sub.add_mutually_exclusive_group()
            capture_mode.add_argument(
                "--raw", action="store_true", help="Capture without targets"
            )
            capture_mode.add_argument(
                "--targets",
                choices=["text", "icons", "accessibility"],
                default="accessibility",
                help="Annotation targets (default: accessibility)",
            )
        elif action in ["click", "query"]:
            sub.add_argument(
                "reference", help="Current screenshot reference, e.g. @a13"
            )
        elif action == "clipboard":
            sub.add_argument("operation", choices=["get", "set"])
        if action == "zoom":
            for coordinate in ["x0", "y0", "x1", "y1"]:
                sub.add_argument(coordinate, type=int)
            sub.add_argument("--output", type=Path)
            sub.add_argument(
                "--scale",
                type=int,
                help="Integer upscale (default: enlarge small crops up to 8x)",
            )
        if action == "repl":
            sub.add_argument(
                "--code", help="Python cell to run (default: read it from stdin)"
            )
            sub.add_argument(
                "--timeout",
                type=float,
                default=300,
                help="Interrupt the cell after this many seconds (default: 300)",
            )
            sub.add_argument(
                "--output-dir",
                type=Path,
                help="Directory for this cell's images (default: the session directory)",
            )
            sub.add_argument(
                "--reset",
                action="store_true",
                help="Discard the REPL's state before running the cell",
            )
    return cli


def main():
    configure_session_environment()
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
    if args.action == "setup-accessibility":
        accessibility_targets.check_dependencies()
        print("Native accessibility dependencies ready")
        return
    if args.action in ["setup-ocr", "setup-icons"]:
        require("uv")
        if args.action == "setup-icons":
            import icon_detector

            icon_detector.setup()
        else:
            ocr_targets.setup()
        print(
            "Local CPU "
            + ("icon" if args.action == "setup-icons" else "OCR")
            + " runtime ready"
        )
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
    if args.action == "prune":
        print(json.dumps(prune(dry=args.dry), indent=2))
        return
    if args.action == "start":
        with interaction_lock(args.name):
            state = start(
                args.name,
                args.size,
                accessibility=args.accessibility,
                screenshot_directory=args.screenshot_directory,
            )
            try:
                screenshot = automatic_screenshot(state, "start")
            except Exception as error:
                raise RuntimeError(
                    f"Session {args.name!r} started, but its screenshot failed: {error}. "
                    "Inspect the session before starting it again"
                ) from error
            print_result(state, screenshot)
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
        interaction = args.action in [
            "launch",
            "browser",
            "exec",
            "input",
            "click",
        ] or (args.action == "clipboard" and args.operation == "set")
        try:
            result = perform(state, args, command)
        except Exception:
            if interaction:
                try:
                    print_result(None, automatic_screenshot(state, args.action))
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    print(
                        f"computer-use: Could not capture after failed interaction: {error}",
                        file=sys.stderr,
                    )
            raise
        if getattr(args, "settle", None) is not None:
            settle(state, args)
        screenshot = None
        if interaction:
            try:
                screenshot = automatic_screenshot(state, args.action)
            except Exception as error:
                if isinstance(result, int) and result != 0:
                    print(
                        f"computer-use: Could not capture after failed command: {error}",
                        file=sys.stderr,
                    )
                    raise SystemExit(result) from None
                raise RuntimeError(
                    f"Interaction completed, but its screenshot failed: {error}. "
                    "Inspect the session before repeating the interaction"
                ) from error
        print_result(result, screenshot)
        if isinstance(result, int):
            raise SystemExit(result)


def perform(state, args, command):
    if args.action in ["launch", "browser"] and args.accessibility:
        accessibility_targets.enable(state, service, save)
    env = dict(os.environ, **environment(state))

    def capture_screen(output):
        capture(env, output)

    if args.action in ["launch", "browser", "exec", "input"] or (
        args.action == "clipboard" and args.operation == "set"
    ):
        ocr_targets.invalidate(state)
    if args.action == "launch":
        return launch(state, args.id, command)
    elif args.action == "browser":
        return browser(state, args)
    elif args.action == "viewer":
        return viewer(state)
    elif args.action in ["exec", "input"]:
        if args.action == "input":
            command = ["xdotool", *command]
        return run_interruptible(state, command, env)
    elif args.action == "screenshot":
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        ocr_targets.screenshot(
            state,
            output,
            capture_screen,
            service,
            raw=args.raw,
            target_mode=args.targets,
        )
        print(output)
    elif args.action == "query":
        snapshot, target = ocr_targets.query(state, args.reference)
        print(
            json.dumps(
                {
                    **target,
                    "image": snapshot["image"],
                    "size": snapshot["size"],
                    "screen_size": snapshot["size"],
                    "image_size": snapshot.get("image_size"),
                    "target_mode": snapshot.get("target_mode", "text"),
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

        ocr_targets.click(state, args.reference, capture_screen, native_click, service)
    elif args.action == "windows":
        print(json.dumps(list_windows(env), indent=2))
    elif args.action == "zoom":
        output = (
            args.output.expanduser().resolve()
            if args.output
            else Path(state["directory"]) / f"zoom-{time.time_ns()}.png"
        )
        print(
            screen.zoom(
                env,
                (args.x0, args.y0, args.x1, args.y1),
                output,
                scale=args.scale,
                size=screen_size(state),
            )
        )
    elif args.action == "clipboard":
        if args.operation == "get":
            sys.stdout.write(get_clipboard(env))
        else:
            set_clipboard(state, sys.stdin.read(), env)
    elif args.action == "repl":
        code = args.code if args.code is not None else sys.stdin.read()
        # Input helpers inside the cell retire references themselves, so a cell
        # may still click a reference from the previous CLI screenshot.
        output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
        reply = repl.call(
            state,
            {
                "code": code,
                "image_dir": str(output_dir) if output_dir else None,
                "cwd": os.getcwd(),
            },
            service,
            HELD_LOCK,
            lambda: stop_marker(state).exists(),
            args.timeout,
            reset=args.reset,
        )
        sys.stdout.write(reply.get("stdout", ""))
        for image in reply.get("images", []):
            print("[image] " + image)
        if reply.get("error"):
            sys.stderr.write(reply["error"])
            raise SystemExit(1)


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
