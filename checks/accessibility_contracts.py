"""Accessibility mode, visible controls and native-target invalidation contracts."""

import contextlib
import io
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import computer_use
import accessibility_targets


def settled_probe(values):
    results = iter(values)
    with patch.object(accessibility_targets.time, "sleep"):
        return accessibility_targets.stable_hit(
            lambda: next(results), time.monotonic() + 5, lambda: None
        )


assert settled_probe(["approximate", "Close", "Close"]) == "Close"
assert settled_probe(["covered button", "overlay", "overlay"]) == "overlay"
assert settled_probe(["first", "second", "third", "third"]) == "third"
print("PASS asynchronous hit tests discard the initial approximation and settle")

parser = computer_use.parser()
with contextlib.redirect_stderr(io.StringIO()):
    try:
        args = parser.parse_args(
            [
                "screenshot",
                "demo",
                "--targets",
                "accessibility",
                "--output",
                "/tmp/a.png",
            ]
        )
    except SystemExit:
        raise AssertionError("Screenshots must accept accessibility targets") from None
assert args.targets == "accessibility"
assert parser.parse_args(["start", "demo", "--accessibility"]).accessibility
args = parser.parse_args(["browser", "demo", "--accessibility", "--no-cdp"])
assert args.accessibility and args.no_cdp
assert parser.parse_args(["launch", "demo", "--accessibility"]).accessibility
assert (
    parser.parse_args(["screenshot", "demo", "--output", "/tmp/a.png"]).targets
    == "text"
)
assert "gi" not in sys.modules and "torch" not in sys.modules
print("PASS optional accessibility mode and native browser flags preserve text default")


def control(**changes):
    return {
        "role": "button",
        "name": "Close",
        "bounds": [10, 10, 50, 50],
        "clip": [0, 0, 200, 100],
        "showing": True,
        "visible": True,
        "enabled": True,
        "path": [0, 1],
        "hit": True,
        **changes,
    }


targets = accessibility_targets.select_targets(
    [
        control(),
        control(path=[0, 2]),
        control(role="section", name="Container"),
        control(name="Disabled", enabled=False),
        control(name="Hidden", showing=False),
        control(name="Covered", hit=False),
        control(name="Offscreen", bounds=[210, 20, 250, 50]),
        control(name="Malformed", bounds=[float("nan"), 0, 20, 20]),
        control(name="Clipped", bounds=[90, 30, 160, 80], clip=[0, 0, 120, 100]),
    ],
    (200, 100),
    {"id": 11, "pid": 22},
)
assert len(targets) == 2, targets
assert targets[0]["text"] == "Close" and targets[0]["kind"] == "accessibility"
assert targets[0]["role"] == "button" and targets[0]["window"] == {"id": 11, "pid": 22}
assert targets[1]["bounds"] == [90, 30, 120, 80]
assert targets[1]["center"] == [104, 54]
print(
    "PASS semantic controls deduplicate and exclude disabled, hidden and occluded regions"
)

state = {"directory": "/tmp/example-session"}
assert (
    computer_use.environment(
        state | {"display": ":90", "xauthority": "x", "bus_socket": "b"}
    )["AT_SPI_BUS_ADDRESS"]
    == ""
)
assert (
    accessibility_targets.bus_address(state)
    == "unix:path=/tmp/example-session/accessibility-bus"
)
print("PASS accessibility bus address is session-local")

with tempfile.TemporaryDirectory() as temporary:
    state = {"directory": temporary, "prefix": "test-a11y", "units": []}

    def failing_start(state, unit, command):
        state["units"].append(unit)
        if unit.endswith("-bus.service"):
            Path(temporary, "accessibility-bus").touch()
        else:
            raise RuntimeError("registry startup failed")

    def still_active(command, **kwargs):
        return SimpleNamespace(returncode=0 if "is-active" in command else 1)

    with (
        patch.object(accessibility_targets, "check_dependencies"),
        patch.object(accessibility_targets, "registry_binary", return_value="registry"),
        patch.object(accessibility_targets.subprocess, "run", side_effect=still_active),
    ):
        try:
            accessibility_targets.enable(state, failing_start, lambda state: None)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Startup failure should be reported")
    assert len(state["units"]) == 2, "Failed stops must retain service ownership"
    assert "accessibility_bus" in state
print("PASS failed accessibility cleanup retains live service ownership")

assert callable(getattr(computer_use, "native_browser_command", None)), (
    "No-CDP launches must bypass configured wrapper flags"
)
with tempfile.TemporaryDirectory() as temporary:
    wrapper = Path(temporary) / "helium-wrapper"
    wrapper.write_text('#!/bin/sh\nexec helium --remote-debugging-port=0 "$@"\n')
    binary = wrapper.with_name("helium")
    binary.write_bytes(b"\x7fELFfixture")
    binary.chmod(0o700)
    with patch.object(computer_use.shutil, "which", return_value=str(wrapper)):
        command = computer_use.native_browser_command()
    assert command[-1] == str(binary)
    assert str(wrapper) not in command and not any(
        "--remote-debugging" in arg for arg in command
    )
print("PASS native browser bypasses configured launcher debugging flags")

assert callable(getattr(accessibility_targets, "read_children", None)), (
    "Tree limits must apply before child enumeration"
)


class HugeNode:
    def get_child_count(self):
        return 5000

    def get_child_at_index(self, index):
        raise AssertionError("Do not enumerate an oversized child list")


try:
    list(accessibility_targets.read_children(HugeNode(), 4000, time.monotonic() + 5))
except RuntimeError as error:
    assert "limit" in str(error)
else:
    raise AssertionError("Oversized children should fail before enumeration")
print("PASS tree limit rejects oversized child lists before remote enumeration")
