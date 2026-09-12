"""Bootstrap contracts: missing dependencies and safe, relocatable command links."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts/setup.py"


class SetupContracts(unittest.TestCase):
    def test_setup_entry_point_exists(self):
        self.assertTrue(
            SETUP.is_file(), "The installed skill needs a setup entry point"
        )

    @unittest.skipUnless(SETUP.is_file(), "setup not implemented yet")
    def test_missing_dependencies_do_not_install_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "commands"
            result = subprocess.run(
                [sys.executable, str(SETUP), "--bin-dir", str(target)],
                env={**os.environ, "PATH": directory},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Xvfb", result.stderr)
            self.assertFalse(target.exists())

    @unittest.skipUnless(SETUP.is_file(), "setup not implemented yet")
    def test_links_are_idempotent_and_preserve_conflicting_files(self):
        spec = importlib.util.spec_from_file_location("bootstrap", SETUP)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        with tempfile.TemporaryDirectory(prefix="skill setup ") as directory:
            target = Path(directory)
            conflict = target / "virtual-browser"
            conflict.write_text("existing user command")
            with self.assertRaisesRegex(RuntimeError, "virtual-browser"):
                setup.install_commands(target)
            self.assertFalse((target / "computer-use").exists())
            self.assertEqual(conflict.read_text(), "existing user command")
            conflict.unlink()
            setup.install_commands(target)
            setup.install_commands(target)
            for name, filename in setup.COMMANDS.items():
                self.assertEqual((target / name).resolve(), ROOT / "scripts" / filename)
            result = subprocess.run(
                [str(target / "computer-use"), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_helper_recovers_login_bus_environment(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import computer_use

        runtime = Path(f"/run/user/{os.getuid()}")
        if not (runtime / "bus").is_socket():
            self.skipTest("requires an existing user login bus")
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(
                hasattr(computer_use, "configure_session_environment"),
                "Helpers must recover an existing login bus in stripped agent shells",
            )
            computer_use.configure_session_environment()
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], str(runtime))
            self.assertEqual(
                os.environ["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={runtime}/bus"
            )
        with patch.dict(
            os.environ,
            {
                "XDG_RUNTIME_DIR": "/custom/runtime",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/custom/bus",
            },
            clear=True,
        ):
            computer_use.configure_session_environment()
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], "/custom/runtime")
            self.assertEqual(
                os.environ["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/custom/bus"
            )

    @unittest.skipUnless(SETUP.is_file(), "setup not implemented yet")
    def test_smoke_failure_still_stops_owned_session(self):
        spec = importlib.util.spec_from_file_location("bootstrap", SETUP)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        calls = []

        def command(*args):
            calls.append(args)
            if args[0] == "screenshot":
                raise RuntimeError("capture failed")
            return "{}"

        with patch.object(setup, "command", side_effect=command):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                setup.smoke_check()
        self.assertEqual(calls[0][0], "start")
        self.assertEqual(calls[-1][0], "stop")
        self.assertEqual(calls[0][1], calls[-1][1])


if __name__ == "__main__":
    unittest.main()
