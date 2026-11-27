"""Prune moves only fully inactive sessions to dated trash and never deletes.

Uses a temporary state root and real transient user units for live, starting,
failed and orphaned cases. Every unit and session here lives in this run's
computer-use-check-<tag>- namespace; the unit listing is limited to it, and a
real prune ignores the computer-use-check- namespace, so the two never touch
each other's units.
"""

import datetime
import fcntl
import json
import os
import secrets
import shutil
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


def transient(unit, *command, collect=True, properties=()):
    subprocess.run(
        [
            "systemd-run",
            "--user",
            *(["--collect"] if collect else []),
            *(f"--property={value}" for value in properties),
            "--no-block",
            "--unit=" + unit,
            "--",
            *command,
        ],
        capture_output=True,
        check=True,
    )


def write_state(directory, prefix, units, **extra):
    (directory / "state.json").write_text(
        json.dumps(
            {
                "name": directory.name,
                "directory": str(directory),
                "prefix": prefix,
                "display_unit": prefix + "-display.service",
                "bus_unit": prefix + "-bus.service",
                "units": units,
                "apps": {},
                **extra,
            }
        )
    )


def state_of(unit):
    return systemctl("show", "-p", "ActiveState", "--value", unit).stdout.strip()


class OnLock:
    """fcntl stand-in that runs an action just before prune locks a session."""

    LOCK_EX, LOCK_NB, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN

    def __init__(self, actions):
        self.actions = actions  # lock file name -> callable, run once

    def flock(self, file, operation):
        action = self.actions.pop(Path(file.name).name, None)
        if action:
            action()
        return fcntl.flock(file, operation)


def main(base):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    root, trash_parent, external = base / "state", base / "trash", base / "shots"
    for directory in (root, trash_parent, external):
        directory.mkdir()
    (external / "keep.png").write_bytes(b"png")
    tag = secrets.token_hex(3)
    space = f"computer-use-check-{tag}-"

    def folder(role):
        return f"check-{tag}-{role}"

    def role(name):
        return name.removeprefix(f"check-{tag}-")

    def session(role_name, units, **extra):
        directory = root / folder(role_name)
        directory.mkdir(mode=0o700)
        prefix = f"computer-use-{directory.name}-{secrets.token_hex(3)}"
        write_state(directory, prefix, [prefix + "-display.service", *units], **extra)
        (directory / "marker.txt").write_text(role_name)
        return prefix

    def mine():
        listed = computer_use.loaded_session_units()
        # A listed unit that is not loaded reads as inactive and must be skipped.
        return [unit for unit in listed if unit.startswith(space)] + [
            space + "ghost.service"
        ]

    live_unit = space + "live.service"
    starting_unit = space + "starting.service"
    pair_unit = space + "pair.service"
    orphan_unit = space + "orphan.service"
    failed_orphan = space + "forphan.service"
    swap_prefix = f"computer-use-{folder('swap')}-{secrets.token_hex(3)}"
    swap_unit = swap_prefix + "-display.service"
    newcomer_unit = (
        f"computer-use-{folder('newcomer')}-{secrets.token_hex(3)}-display.service"
    )
    # Session `pair` stays; this belongs to a gone `pair-2` and is an orphan.
    pair_orphan = (
        f"computer-use-{folder('pair')}-2-{secrets.token_hex(3)}-display.service"
    )
    # Hidden from the sealed listing: only the session path can reset it.
    failed_unit = f"computer-use-check-hidden{secrets.token_hex(4)}.service"
    started = []
    day = datetime.date(2026, 11, 27)
    trash = trash_parent / "tmp-trash-20261127"
    try:
        transient(failed_unit, "false", collect=False)
        transient(failed_orphan, "false", collect=False)
        started += [failed_unit, failed_orphan]
        for unit in (live_unit, pair_unit, orphan_unit, swap_unit, newcomer_unit):
            transient(unit, "sleep", "300")
            started.append(unit)
        transient(pair_orphan, "sleep", "300")
        started.append(pair_orphan)
        # Type=notify without a notification stays "activating".
        transient(starting_unit, "sleep", "300", properties=["Type=notify"])
        started.append(starting_unit)
        computer_use.poll(
            lambda: (
                state_of(failed_unit) == "failed"
                and state_of(failed_orphan) == "failed"
                and state_of(starting_unit) == "activating"
            ),
            "fixture units did not reach their states",
        )

        session("dead", [], screenshot_directory=str(external))
        session("dead-failed", [failed_unit])
        session("broken", [])
        live_prefix = session("live", [live_unit])
        extra_unit = live_prefix + "-extra.service"
        transient(extra_unit, "sleep", "300")
        started.append(extra_unit)
        session("starting", [starting_unit])
        session("busy", [])
        session("swap", [])
        session("pair", [pair_unit])
        session("vanish", [])
        session("sibling", [])
        sibling = json.loads((root / folder("sibling") / "state.json").read_text())
        sibling["directory"] = str(root / folder("dead"))
        (root / folder("sibling") / "state.json").write_text(json.dumps(sibling))
        session("badunits", [123])
        (root / folder("incomplete")).mkdir()
        (trash / f"computer-use-{folder('dead')}").mkdir(parents=True)
        (trash / f"computer-use-{folder('dead-failed')}").symlink_to(base / "missing")

        def listing_with_newcomer():
            # A session that starts while prune runs creates its folder first.
            (root / folder("newcomer")).mkdir(exist_ok=True)
            return mine()

        (root / ".locks").mkdir(mode=0o700)
        with (root / ".locks" / (folder("busy") + ".lock")).open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            dry = computer_use.prune(
                dry=True,
                root=root,
                trash_parent=trash_parent,
                day=day,
                list_units=mine,
                ignore=(),
            )
            assert sorted(map(role, dry["prune"])) == [
                "broken",
                "dead",
                "dead-failed",
                "swap",
                "vanish",
            ], dry
            kept = {role(item["name"]): item["reason"] for item in dry["kept"]}
            assert set(kept) == {
                "live",
                "starting",
                "busy",
                "pair",
                "sibling",
                "badunits",
                "incomplete",
            }, kept
            assert "busy" in kept["busy"] and starting_unit in kept["starting"], kept
            assert "another directory" in kept["sibling"], kept
            assert "units" in kept["badunits"], kept
            assert state_of(failed_orphan) == "failed", "--dry must not reset"
            assert state_of(failed_unit) == "failed", "--dry must not reset"
            orphans = {item["unit"]: item["state"] for item in dry["orphan_units"]}
            # The newcomer has no folder yet here; the real run creates it.
            assert orphans == {
                orphan_unit: "active",
                failed_orphan: "failed",
                newcomer_unit: "active",
                pair_orphan: "active",
            }, orphans
            assert all((root / name).is_dir() for name in dry["prune"])
            print("PASS --dry lists exactly the dead sessions and changes nothing")

            real_flock, real_exists = computer_use.fcntl, Path.exists
            real_rename = computer_use.rename_noreplace
            parked = base / "parked"

            def failing_rename(source, destination):
                if Path(source).name == folder("broken"):
                    raise OSError(5, "Input/output error", str(destination))
                return real_rename(source, destination)

            computer_use.fcntl = OnLock(
                {
                    # stop + start of the same name between enumeration and lock.
                    folder("swap") + ".lock": lambda: write_state(
                        root / folder("swap"), swap_prefix, [swap_unit]
                    ),
                    # stop removes the folder between enumeration and lock.
                    folder("vanish") + ".lock": lambda: shutil.move(
                        root / folder("vanish"), parked
                    ),
                }
            )
            # An exists-then-move prune would trust this lie and nest the move;
            # the atomic rename does not ask.
            Path.exists = lambda self, **_kwargs: False
            computer_use.rename_noreplace = failing_rename
            try:
                result = computer_use.prune(
                    root=root,
                    trash_parent=trash_parent,
                    day=day,
                    list_units=listing_with_newcomer,
                    ignore=(),
                )
            finally:
                computer_use.fcntl, Path.exists = real_flock, real_exists
                computer_use.rename_noreplace = real_rename
        moved = {role(item["name"]): Path(item["trash"]) for item in result["pruned"]}
        assert sorted(moved) == ["dead", "dead-failed"], result
        kept = {role(item["name"]): item["reason"] for item in result["kept"]}
        assert "swap" in kept and swap_unit in kept["swap"], kept
        assert (root / folder("swap")).is_dir(), "a replaced live session was moved"
        assert "vanish" not in kept and parked.is_dir(), kept
        print("PASS sessions replaced or removed before their lock are judged anew")

        assert not any((trash / f"computer-use-{folder('dead')}").iterdir())
        assert moved["dead"] == trash / f"computer-use-{folder('dead')}-2", moved
        assert (moved["dead"] / "marker.txt").read_text() == "dead"
        assert (trash / f"computer-use-{folder('dead-failed')}").is_symlink()
        assert moved["dead-failed"].name == f"computer-use-{folder('dead-failed')}-2"
        assert (external / "keep.png").read_bytes() == b"png"
        assert state_of(failed_unit) != "failed", "session units were not reset"
        print("PASS dead sessions take a free trash slot atomically; units reset")

        assert "error" in kept["broken"] and (root / folder("broken")).is_dir()
        assert "another directory" in kept["sibling"], kept
        assert (root / folder("sibling") / "marker.txt").read_text() == "sibling"
        for name in ("live", "starting", "busy", "incomplete", "pair", "badunits"):
            assert (root / folder(name)).is_dir(), name
        assert (
            state_of(live_unit) == "active" and state_of(starting_unit) == "activating"
        )
        orphans = {item["unit"]: item["state"] for item in result["orphan_units"]}
        assert orphans == {orphan_unit: "active", pair_orphan: "active"}, orphans
        assert result["reset_units"] == [failed_orphan], result
        assert state_of(failed_orphan) != "failed"
        assert state_of(orphan_unit) == "active" and state_of(extra_unit) == "active"
        print("PASS errors, odd state, live, starting and new sessions are kept")

        default = computer_use.prune(
            dry=True, root=root, trash_parent=trash_parent, list_units=mine
        )
        assert default["orphan_units"] == [], default["orphan_units"]
        print("PASS a real prune ignores the checks' own unit namespace")

        def failing_listing():
            raise subprocess.CalledProcessError(1, ["systemctl"])

        partial = computer_use.prune(
            dry=True, root=root, trash_parent=trash_parent, list_units=failing_listing
        )
        assert partial["orphan_units"] is None and "orphan_error" in partial, partial
        assert folder("broken") in partial["prune"], partial
        print("PASS a failed unit listing still returns the session report")
    finally:
        for unit in started:
            systemctl("stop", unit)
            systemctl("reset-failed", unit)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-prune-check-") as temporary:
        main(Path(temporary))
