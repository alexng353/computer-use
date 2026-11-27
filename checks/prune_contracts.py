"""Prune moves only fully inactive sessions to dated trash and never deletes.

Uses a temporary state root and real transient user units for live, starting,
failed and orphaned cases. Unit listing is limited to this check's own units,
so real sessions are never reset or reported.
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


def session(root, name, tag, units, **extra):
    directory = root / name
    directory.mkdir(mode=0o700)
    prefix = f"computer-use-{name}-{tag}{secrets.token_hex(2)}"
    write_state(directory, prefix, [prefix + "-display.service", *units], **extra)
    (directory / "marker.txt").write_text(name)
    return prefix


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


class SwapOnLock:
    """fcntl stand-in: replaces the `swap` session just as prune locks it."""

    LOCK_EX, LOCK_NB, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN

    def __init__(self, directory, prefix, units):
        self.directory, self.prefix, self.units = directory, prefix, units
        self.swapped = False

    def flock(self, file, operation):
        if file.name.endswith("/swap.lock") and not self.swapped:
            # stop + start of the same name between enumeration and the lock.
            write_state(self.directory, self.prefix, self.units)
            self.swapped = True
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
    tag = "pc" + secrets.token_hex(3)

    def mine():
        return [unit for unit in computer_use.loaded_session_units() if tag in unit]

    live_unit = f"computer-use-prune-live-{tag}.service"
    starting_unit = f"computer-use-prune-starting-{tag}.service"
    swap_unit = f"computer-use-swap-{tag}-new-display.service"
    orphan_unit = f"computer-use-prune-orphan-{tag}.service"
    failed_orphan = f"computer-use-prune-forphan-{tag}.service"
    newcomer_unit = f"computer-use-newcomer-{tag}-display.service"
    # Hidden from the sealed listing: only the session path can reset it.
    failed_unit = f"computer-use-prunefail-{secrets.token_hex(4)}.service"
    started = [failed_unit]
    day = datetime.date(2026, 11, 27)
    trash = trash_parent / "tmp-trash-20261127"
    try:
        transient(failed_unit, "false", collect=False)
        transient(failed_orphan, "false", collect=False)
        for unit in (live_unit, orphan_unit, swap_unit, newcomer_unit):
            transient(unit, "sleep", "300")
        # Type=notify without a notification stays "activating".
        transient(starting_unit, "sleep", "300", properties=["Type=notify"])
        started += [failed_orphan, live_unit, orphan_unit, swap_unit, newcomer_unit]
        started.append(starting_unit)
        computer_use.poll(
            lambda: (
                state_of(failed_unit) == "failed"
                and state_of(failed_orphan) == "failed"
                and state_of(starting_unit) == "activating"
            ),
            "fixture units did not reach their states",
        )

        session(root, "dead", tag, [], screenshot_directory=str(external))
        session(root, "dead-failed", tag, [failed_unit])
        session(root, "broken", tag, [])
        live_prefix = session(root, "live", tag, [live_unit])
        extra_unit = live_prefix + "-extra.service"
        transient(extra_unit, "sleep", "300")
        started.append(extra_unit)
        session(root, "starting", tag, [starting_unit])
        session(root, "busy", tag, [])
        session(root, "swap", tag, [])
        (root / "incomplete").mkdir()
        (trash / "computer-use-dead").mkdir(parents=True)
        (trash / "computer-use-dead-failed").symlink_to(base / "missing")

        def listing_with_newcomer():
            # A session that starts while prune runs creates its folder first.
            (root / f"newcomer-{tag}").mkdir(exist_ok=True)
            return mine()

        (root / ".locks").mkdir(mode=0o700)
        with (root / ".locks" / "busy.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            dry = computer_use.prune(
                dry=True,
                root=root,
                trash_parent=trash_parent,
                day=day,
                list_units=mine,
            )
            assert sorted(dry["prune"]) == [
                "broken",
                "dead",
                "dead-failed",
                "swap",
            ], dry
            kept = {item["name"]: item["reason"] for item in dry["kept"]}
            assert set(kept) == {"live", "starting", "busy", "incomplete"}, kept
            assert "busy" in kept["busy"] and starting_unit in kept["starting"], kept
            assert state_of(failed_orphan) == "failed", "--dry must not reset"
            assert state_of(failed_unit) == "failed", "--dry must not reset"
            orphans = {item["unit"]: item["state"] for item in dry["orphan_units"]}
            # The newcomer has no folder yet here; the real run creates it.
            assert orphans == {
                orphan_unit: "active",
                failed_orphan: "failed",
                newcomer_unit: "active",
            }, orphans
            assert all((root / name).is_dir() for name in dry["prune"])
            print("PASS --dry lists exactly the dead sessions and changes nothing")

            real_flock, real_exists = computer_use.fcntl, Path.exists
            real_rename = getattr(computer_use, "rename_noreplace", None)

            def failing_rename(source, destination):
                if Path(source).name == "broken":
                    raise OSError(5, "Input/output error", str(destination))
                return real_rename(source, destination)

            computer_use.fcntl = SwapOnLock(
                root / "swap", f"computer-use-swap-{tag}-new", [swap_unit]
            )
            # Another prune can take a free-looking slot between check and move.
            Path.exists = lambda self, **_kwargs: False
            if real_rename:
                computer_use.rename_noreplace = failing_rename
            try:
                result = computer_use.prune(
                    root=root,
                    trash_parent=trash_parent,
                    day=day,
                    list_units=listing_with_newcomer,
                )
            finally:
                computer_use.fcntl, Path.exists = real_flock, real_exists
                if real_rename:
                    computer_use.rename_noreplace = real_rename
        moved = {item["name"]: Path(item["trash"]) for item in result["pruned"]}
        assert sorted(moved) == ["dead", "dead-failed"], result
        kept = {item["name"]: item["reason"] for item in result["kept"]}
        assert "swap" in kept and swap_unit in kept["swap"], kept
        assert (root / "swap").is_dir(), "a replaced live session was moved"
        print("PASS a session replaced before its lock is judged by its new units")

        assert not any((trash / "computer-use-dead").iterdir()), "moved into a slot"
        assert moved["dead"] == trash / "computer-use-dead-2", moved
        assert (moved["dead"] / "marker.txt").read_text() == "dead"
        assert (trash / "computer-use-dead-failed").is_symlink()
        assert moved["dead-failed"] == trash / "computer-use-dead-failed-2", moved
        assert (external / "keep.png").read_bytes() == b"png"
        assert state_of(failed_unit) != "failed", "session units were not reset"
        print("PASS dead sessions take a free trash slot atomically; units reset")

        assert "broken" in kept and "error" in kept["broken"], kept
        assert (root / "broken").is_dir()
        for name in ("live", "starting", "busy", "incomplete"):
            assert (root / name).is_dir(), name
        assert (
            state_of(live_unit) == "active" and state_of(starting_unit) == "activating"
        )
        orphans = {item["unit"]: item["state"] for item in result["orphan_units"]}
        assert orphans == {orphan_unit: "active"}, orphans
        assert result["reset_units"] == [failed_orphan], result
        assert state_of(failed_orphan) != "failed"
        assert state_of(orphan_unit) == "active" and state_of(extra_unit) == "active"
        print("PASS errors, live, starting and new sessions are kept and reported")

        def failing_listing():
            raise subprocess.CalledProcessError(1, ["systemctl"])

        partial = computer_use.prune(
            dry=True, root=root, trash_parent=trash_parent, list_units=failing_listing
        )
        assert partial["orphan_units"] is None and "orphan_error" in partial, partial
        assert "broken" in partial["prune"], partial
        print("PASS a failed unit listing still returns the session report")
    finally:
        for unit in started:
            systemctl("stop", unit)
            systemctl("reset-failed", unit)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-prune-check-") as temporary:
        main(Path(temporary))
