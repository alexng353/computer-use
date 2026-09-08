"""Optional native AT-SPI targets on a session-owned accessibility bus."""

import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from xml.sax.saxutils import escape

PYTHON = Path("/usr/bin/python3")
ROLES = {
    "button",
    "link",
    "entry",
    "combo box",
    "check box",
    "radio button",
    "toggle button",
    "menu item",
    "check menu item",
    "radio menu item",
    "page tab",
    "slider",
    "spin button",
    "password text",
}
CLIPPING_ROLES = {
    "frame",
    "window",
    "dialog",
    "document web",
    "scroll pane",
    "viewport",
}


def bus_address(state):
    return "unix:path=" + str(Path(state["directory"]) / "accessibility-bus")


def registry_binary():
    for candidate in (
        "/usr/lib/at-spi2-registryd",
        "/usr/libexec/at-spi2-registryd",
        "/usr/lib/at-spi2-core/at-spi2-registryd",
    ):
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("Install at-spi2-core to enable accessibility targets")


def check_dependencies():
    registry_binary()
    for binary in ("dbus-daemon", "busctl", "xdotool"):
        if not shutil.which(binary):
            raise RuntimeError(f"Accessibility targets require {binary}")
    result = subprocess.run(
        [
            str(PYTHON),
            "-c",
            "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi; from PIL import Image",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Accessibility targets require system Python with PyGObject, AT-SPI introspection and Pillow (Arch: python-gobject at-spi2-core python-pillow)"
        )


def services_alive(state):
    return all(
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", state["prefix"] + suffix],
            check=False,
            capture_output=True,
        ).returncode
        == 0
        for suffix in ("-accessibility-bus.service", "-accessibility-registry.service")
    )


def enable(state, start_service, save_state):
    if state.get("accessibility_bus"):
        if state["accessibility_bus"] != bus_address(state) or not services_alive(
            state
        ):
            raise RuntimeError(
                "Accessibility bus stopped; recreate the session and relaunch its apps"
            )
        return
    check_dependencies()
    address = bus_address(state)
    config = Path(state["directory"]) / "accessibility-bus.conf"
    # No host bus forwarding or activation: the registry is separately owned.
    config.write_text(f'''<busconfig>
<type>accessibility</type><listen>{escape(address)}</listen><auth>EXTERNAL</auth>
<policy context="default"><allow user="{os.getuid()}"/>
<allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy>
</busconfig>''')
    state["accessibility_bus"] = address
    units = [
        state["prefix"] + suffix
        for suffix in ("-accessibility-bus.service", "-accessibility-registry.service")
    ]
    try:
        start_service(
            state, units[0], ["dbus-daemon", "--nofork", "--config-file=" + str(config)]
        )
        deadline = time.monotonic() + 10
        while not Path(state["directory"], "accessibility-bus").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Private accessibility bus did not start")
            time.sleep(0.05)
        start_service(state, units[1], [registry_binary()])
        while subprocess.run(
            [
                "busctl",
                "--address=" + address,
                "--timeout=1",
                "status",
                "org.a11y.atspi.Registry",
            ],
            capture_output=True,
            check=False,
        ).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError("Private accessibility registry did not start")
            time.sleep(0.05)
    except Exception as error:
        live = []
        for unit in reversed(units):
            subprocess.run(
                ["systemctl", "--user", "stop", unit], capture_output=True, check=False
            )
            if (
                subprocess.run(
                    ["systemctl", "--user", "is-active", "--quiet", unit],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            ):
                live.append(unit)
            elif unit in state["units"]:
                state["units"].remove(unit)
        if not live:
            state.pop("accessibility_bus", None)
        save_state(state)
        if live:
            raise RuntimeError(
                "Accessibility cleanup failed; retained live services: "
                + ", ".join(live)
            ) from error
        raise


def intersection(a, b):
    return [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]


def clipped(record, size):
    bounds = record.get("bounds")
    clip = record.get("clip", [0, 0, *size])
    if (
        not bounds
        or len(bounds) != 4
        or len(clip) != 4
        or not all(math.isfinite(v) for v in (*bounds, *clip))
    ):
        return None
    bounds = intersection(intersection(bounds, clip), [0, 0, *size])
    return bounds if bounds[0] < bounds[2] and bounds[1] < bounds[3] else None


def select_targets(records, size, window):
    targets, seen = [], set()
    for record in records:
        if record["role"] not in ROLES or not all(
            record.get(key) for key in ("showing", "visible", "enabled", "hit")
        ):
            continue
        bounds = clipped(record, size)
        if bounds is None:
            continue
        key = (record["role"], record["name"], *bounds)
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            {
                "kind": "accessibility",
                "text": record["name"],
                "role": record["role"],
                "confidence": None,
                "bounds": bounds,
                "center": [
                    (bounds[0] + bounds[2] - 1) // 2,
                    (bounds[1] + bounds[3] - 1) // 2,
                ],
                "path": record["path"],
                "window": window,
            }
        )
    return targets


def focused_window():
    def query(*args):
        return subprocess.run(
            ["xdotool", *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        window = query("getwindowfocus")
        geometry = dict(
            line.split("=", 1)
            for line in query("getwindowgeometry", "--shell", window).splitlines()
        )
        x, y, width, height = (
            int(geometry[key]) for key in ("X", "Y", "WIDTH", "HEIGHT")
        )
        return {
            "id": int(window),
            "pid": int(query("getwindowpid", window)),
            "name": query("getwindowname", window),
            "bounds": [x, y, x + width, y + height],
        }
    except (subprocess.CalledProcessError, ValueError, KeyError) as error:
        raise RuntimeError(
            "Focus a supported app window before taking accessibility targets"
        ) from error


def check_budget(deadline):
    if time.monotonic() > deadline:
        raise RuntimeError(
            "Accessibility tree exceeded its read limit; use text targets on this screen"
        )


def read_children(node, remaining, deadline):
    check_budget(deadline)
    count = node.get_child_count()
    if count < 0 or count > remaining:
        raise RuntimeError(
            "Accessibility tree exceeded its node limit; use text targets on this screen"
        )
    for index in range(count):
        check_budget(deadline)
        yield index, node.get_child_at_index(index)


def stable_hit(probe, deadline, pump):
    # Chromium starts an async renderer hit test but returns an approximation on
    # the first call. Repeated reads reduce misses, but AT-SPI exposes no renderer
    # completion signal: equal replies can still be cached approximations.
    check_budget(deadline)
    probe()
    previous = None
    for attempt in range(5):
        check_budget(deadline)
        time.sleep(0.01)
        pump()
        current = probe()
        if attempt and current == previous:
            return current
        previous = current
    return None


def engine(directory):
    def detect(image):
        # libatspi caches remote objects across reads. A fresh reader avoids
        # retaining destroyed Chromium nodes after navigation or text replacement.
        try:
            result = subprocess.run(
                [str(PYTHON), __file__, str(directory), *map(str, image.size)],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "Accessibility reader timed out; take a fresh screenshot or use text targets"
            ) from error
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Accessibility reader failed")
        return json.loads(result.stdout)

    return detect


def native_reader(directory):
    expected = bus_address({"directory": directory})
    if os.environ.get("AT_SPI_BUS_ADDRESS") != expected:
        raise RuntimeError("Accessibility worker requires its session's private bus")
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib
    from x11_stack import occluders

    Atspi.set_timeout(750, 750)

    def pump():
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                break
            context.iteration(False)

    def bounds(node):
        rect = node.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
        return [rect.x, rect.y, rect.x + rect.width, rect.y + rect.height]

    def detect(image):
        deadline = time.monotonic() + 5
        focus = focused_window()
        covering = occluders(focus["id"])
        pump()
        desktop = Atspi.get_desktop(0)
        frames = []
        for i, app in read_children(desktop, 4000, deadline):
            if app.get_process_id() != focus["pid"]:
                continue
            for j, frame in read_children(app, 4000, deadline):
                if (
                    frame.is_component()
                    and frame.get_name() == focus["name"]
                    and all(
                        abs(a - b) <= 2
                        for a, b in zip(bounds(frame), focus["bounds"], strict=True)
                    )
                ):
                    frames.append((frame, [i, j]))
        if len(frames) != 1:
            raise RuntimeError(
                "Focused window is not uniquely available through accessibility; launch it with --accessibility (existing apps need relaunching)"
            )
        frame, path = frames[0]

        def hit_matches(node, center):
            if any(
                x1 <= center[0] < x2 and y1 <= center[1] < y2
                for _, x1, y1, x2, y2 in covering
            ):
                return False

            def probe():
                hit = frame
                for _ in range(32):
                    check_budget(deadline)
                    if not hit.is_component():
                        break
                    child = hit.get_component_iface().get_accessible_at_point(
                        *center, Atspi.CoordType.SCREEN
                    )
                    if child is None or child == hit:
                        return hit
                    hit = child
                return None

            hit = stable_hit(probe, deadline, pump)
            for _ in range(64):
                check_budget(deadline)
                if hit == node:
                    return True
                if hit is None or hit == frame:
                    return False
                hit = hit.get_parent()
            return False

        queue = deque(
            [(frame, path, intersection(focus["bounds"], [0, 0, *image.size]))]
        )
        records = []
        count = 0
        while queue:
            if count >= 4000 or time.monotonic() > deadline:
                raise RuntimeError(
                    "Accessibility tree exceeded its read limit; use text targets on this screen"
                )
            node, path, clip = queue.popleft()
            count += 1
            if len(path) > 80:
                raise RuntimeError(
                    "Accessibility tree is too deep; use text targets on this screen"
                )
            role = node.get_role_name()
            node_bounds = bounds(node) if node.is_component() else None
            if role in CLIPPING_ROLES and node_bounds:
                clip = intersection(clip, node_bounds)
            if role in ROLES and node_bounds:
                states = node.get_state_set()
                record = {
                    "role": role,
                    "name": node.get_name(),
                    "bounds": node_bounds,
                    "clip": clip,
                    "path": path,
                    "showing": states.contains(Atspi.StateType.SHOWING),
                    "visible": states.contains(Atspi.StateType.VISIBLE),
                    "enabled": states.contains(Atspi.StateType.ENABLED),
                    "hit": False,
                }
                visible = clipped(record, image.size)
                if visible and all(
                    record[key] for key in ("showing", "visible", "enabled")
                ):
                    center = [
                        (visible[0] + visible[2] - 1) // 2,
                        (visible[1] + visible[3] - 1) // 2,
                    ]
                    record["hit"] = hit_matches(node, center)
                records.append(record)
            queue.extend(
                (child, [*path, i], clip)
                for i, child in read_children(node, 4000 - count - len(queue), deadline)
            )
        if focused_window() != focus or occluders(focus["id"]) != covering:
            raise RuntimeError(
                "Focused window changed during accessibility capture; try again"
            )
        return select_targets(
            records, image.size, {key: focus[key] for key in ("id", "pid")}
        )

    return detect


if __name__ == "__main__":
    from types import SimpleNamespace

    try:
        print(
            json.dumps(
                native_reader(sys.argv[1])(
                    SimpleNamespace(size=tuple(map(int, sys.argv[2:4])))
                )
            )
        )
    except Exception as error:  # noqa: BLE001 - Report native reader failures to its owner.
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
