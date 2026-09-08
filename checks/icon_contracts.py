"""Target-mode defaults, optional imports, geometry and reference contracts."""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import computer_use
import icon_detector
import ocr_targets
import ocr_worker

parser = computer_use.parser()
args = parser.parse_args(["screenshot", "demo", "--output", "/tmp/demo.png"])
assert getattr(args, "targets", None) == "accessibility"
args = parser.parse_args(
    ["screenshot", "demo", "--targets", "text", "--output", "/tmp/demo.png"]
)
assert args.targets == "text"
args = parser.parse_args(
    ["screenshot", "demo", "--targets", "icons", "--output", "/tmp/demo.png"]
)
assert args.targets == "icons"
for flags in (["--targets", "combined"], ["--raw", "--targets", "icons"]):
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(
                ["screenshot", "demo", "--output", "/tmp/demo.png", *flags]
            )
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError(f"Invalid mode accepted: {flags}")
assert parser.parse_args(["setup-icons"]).action == "setup-icons"
assert "torch" not in sys.modules and "ultralytics" not in sys.modules
print(
    "PASS accessibility default, explicit fallbacks, incompatible flags and optional imports"
)

assert "torch" not in sys.modules and "ultralytics" not in sys.modules
targets = icon_detector.targets_from_detections(
    [
        ([0, 0, 200, 100], 0.8),  # Container around the more precise control.
        ([-2, 10.2, 30.1, 40.3], 0.9),
        ([50, 20, 75, 45], 0.14),
        ([float("nan"), 0, 10, 10], 0.9),
        ([40, 20, 50, 30], float("nan")),
        ([90, 50, 80, 60], 0.9),
        ([200, 10, 230, 20], 0.9),
    ],
    (200, 100),
)
assert len(targets) == 1, targets
assert targets[0]["bounds"] == [0, 10, 31, 41]
assert targets[0]["center"] == [15, 25]
assert targets[0]["kind"] == "visual" and targets[0]["text"] is None
print(
    "PASS visual bounds clip to source pixels and reject malformed/low-confidence boxes"
)

with tempfile.TemporaryDirectory(prefix="computer-use-icons-") as temporary:
    root = Path(temporary)
    source, output = root / "source.png", root / "output.png"
    Image.new("RGB", (200, 100), "white").save(source)
    result = ocr_worker.recognize_icons(
        lambda image: targets,
        {"source": str(source), "output": str(output), "prefix": "b"},
    )
    assert result["targets"][0]["ref"] == "b1"
    assert result["size"] == [200, 100]
    with Image.open(source) as raw, Image.open(output) as annotated:
        assert (
            raw.crop((0, 10, 31, 41)).tobytes()
            == annotated.crop((0, 10, 31, 41)).tobytes()
        )
    print("PASS icon annotation preserves target pixels and original coordinates")

    state = {"directory": str(root)}
    snapshot = {
        **result,
        "prefix": "b",
        "source": str(source),
        "image": str(output),
        "target_mode": "icons",
    }
    (root / "ocr-snapshot.json").write_text(json.dumps(snapshot))
    with (
        patch.object(ocr_targets, "ensure_worker") as ensure,
        patch.object(ocr_targets, "request", return_value={"matches": True}) as request,
    ):
        clicks = []
        ocr_targets.click(
            state,
            "@b1",
            lambda path: Image.open(source).save(path),
            lambda x, y: clicks.append((x, y)),
            None,
        )
        assert ensure.call_args.kwargs["target_mode"] == "icons"
        assert request.call_args.kwargs["target_mode"] == "icons"
        assert clicks == [(15, 25)]
        assert not (root / "ocr-snapshot.json").exists()
    print("PASS icon clicks verify with the correct worker and invalidate references")

    (root / "ocr-snapshot.json").write_text(json.dumps(snapshot))
    with (
        patch.object(ocr_targets, "ensure_worker"),
        patch.object(ocr_targets, "request", return_value={"matches": False}),
    ):
        try:
            ocr_targets.click(
                state,
                "@b1",
                lambda path: Image.open(source).save(path),
                lambda *_: (_ for _ in ()).throw(
                    AssertionError("Must not click changed pixels")
                ),
                None,
            )
        except RuntimeError as error:
            assert "pixels changed" in str(error)
        else:
            raise AssertionError("Changed icon pixels were accepted")
        assert not (root / "ocr-snapshot.json").exists()
    print("PASS changed icon pixels reject native input and retire references")

    delayed = []

    def worker_request(state, message, target_mode="text"):
        with Image.open(message["source"]) as source:
            image = source.convert("RGB")
        if target_mode == "icons":
            delayed.append((image, message))
            raise TimeoutError("Slow icon detector")
        return ocr_worker.publish(image, [], message)

    with (
        patch.object(ocr_targets, "ensure_worker"),
        patch.object(ocr_targets, "request", side_effect=worker_request),
    ):
        try:
            ocr_targets.screenshot(
                state,
                output,
                lambda path: Image.new("RGB", (200, 100), "red").save(path),
                None,
                target_mode="icons",
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("Slow icon capture should time out")
        assert not (root / "ocr-snapshot.json").exists()
        ocr_targets.screenshot(
            state,
            output,
            lambda path: Image.new("RGB", (200, 100), "blue").save(path),
            None,
        )
        # Finish the abandoned render after the following text capture succeeds.
        image, message = delayed.pop()
        try:
            ocr_worker.publish(image, [], message)
        except FileNotFoundError:
            pass
        with Image.open(output) as rendered:
            assert rendered.getpixel((0, 0)) == (0, 0, 255), (
                "Late icon render overwrote the current text screenshot"
            )
        snapshot = json.loads((root / "ocr-snapshot.json").read_text())
        assert snapshot["target_mode"] == "text"
        with Image.open(snapshot["source"]) as captured:
            assert captured.getpixel((0, 0)) == (0, 0, 255)
        assert not list(root.glob(".capture-*"))
    print("PASS timed-out icon render cannot overwrite a later text capture")
