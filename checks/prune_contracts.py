"""Prune moves only fully inactive sessions to dated trash and never deletes.

Uses a temporary state root, real transient user units for live, failed and
orphaned cases, and a held interaction lock for a session that is mid-command.
"""

import datetime
import fcntl
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import computer_use


def systemctl(*args):
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def transient(unit, *command, collect=True):
    subprocess.run(
        [
            "systemd-run",
            "--user",
            *(["--collect"] if collect else []),
            "--unit=" + unit,
            "--",
            *command,
        ],
        capture_output=True,
        check=True,
    )


def session(root, name, units, **extra):
    directory = root / name
    directory.mkdir(mode=0o700)
    prefix = f"computer-use-{name}-{secrets.token_hex(3)}"
    (directory / "state.json").write_text(
        json.dumps(
            {
                "name": name,
                "directory": str(directory),
                "prefix": prefix,
                "display_unit": prefix + "-display.service",
                "bus_unit": prefix + "-bus.service",
                "units": [prefix + "-display.service", *units],
                "apps": {},
                **extra,
            }
        )
    )
    (directory / "marker.txt").write_text(name)
    return prefix


def main(base):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    root, trash_parent, external = base / "state", base / "trash", base / "shots"
    root.mkdir()
    trash_parent.mkdir()
    external.mkdir()
    (external / "keep.png").write_bytes(b"png")
    tag = secrets.token_hex(3)
    live_unit = f"computer-use-prune-live-{tag}.service"
    failed_unit = f"computer-use-prune-failed-{tag}.service"
    orphan_unit = f"computer-use-prune-orphan-{tag}.service"
    started = []
    try:
        transient(live_unit, "sleep", "300")
        started.append(live_unit)
        transient(failed_unit, "false", collect=False)
        started.append(failed_unit)
        transient(orphan_unit, "sleep", "300")
        started.append(orphan_unit)
        computer_use.poll(
            lambda: systemctl("is-failed", "--quiet", failed_unit).returncode == 0,
            "failed unit did not fail",
        )

        session(root, "dead", [], screenshot_directory=str(external))
        session(root, "dead-failed", [failed_unit])
        session(root, "live", [live_unit])
        session(root, "busy", [])
        (root / "incomplete").mkdir()
        day = datetime.date(2026, 11, 27)
        trash = trash_parent / "tmp-trash-20261127"
        (trash / "computer-use-dead").mkdir(parents=True)

        (root / ".locks").mkdir(mode=0o700)
        with (root / ".locks" / "busy.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            dry = computer_use.prune(
                dry=True, root=root, trash_parent=trash_parent, day=day
            )
            assert sorted(dry["prune"]) == ["dead", "dead-failed"], dry
            kept = {item["name"]: item["reason"] for item in dry["kept"]}
            assert set(kept) == {"live", "busy", "incomplete"}, kept
            assert "active" in kept["live"] and "busy" in kept["busy"], kept
            assert (root / "dead").is_dir() and (root / "dead-failed").is_dir()
            assert systemctl("is-failed", "--quiet", failed_unit).returncode == 0
            print("PASS --dry lists exactly the dead sessions and changes nothing")

            result = computer_use.prune(root=root, trash_parent=trash_parent, day=day)
        assert sorted(item["name"] for item in result["pruned"]) == [
            "dead",
            "dead-failed",
        ], result
        assert not (root / "dead").exists() and not (root / "dead-failed").exists()
        # An existing trash entry is never overwritten.
        assert (trash / "computer-use-dead").is_dir()
        assert not any((trash / "computer-use-dead").iterdir())
        moved = trash / "computer-use-dead-2"
        assert (moved / "marker.txt").read_text() == "dead", list(trash.iterdir())
        assert (trash / "computer-use-dead-failed" / "marker.txt").is_file()
        assert (external / "keep.png").read_bytes() == b"png"
        for name in ("live", "busy", "incomplete"):
            assert (root / name).is_dir(), name
        assert systemctl("is-failed", "--quiet", failed_unit).returncode != 0
        assert (
            "not-found"
            in systemctl("show", "-p", "LoadState", "--value", failed_unit).stdout
            or systemctl("is-active", failed_unit).stdout.strip() == "inactive"
        )
        print("PASS prune moves dead sessions to dated trash and resets failed units")

        assert systemctl("is-active", "--quiet", live_unit).returncode == 0
        assert orphan_unit in result["orphan_units"], result["orphan_units"]
        assert live_unit not in result["orphan_units"]
        assert systemctl("is-active", "--quiet", orphan_unit).returncode == 0
        print("PASS live sessions and active orphan units are reported, not touched")
    finally:
        for unit in started:
            systemctl("stop", unit)
            systemctl("reset-failed", unit)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-prune-check-") as temporary:
        main(Path(temporary))
