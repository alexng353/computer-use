"""Merge rules and source handling for --targets all, without a desktop."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import icon_detector
import ocr_targets


def target(kind, bounds):
    return {"kind": kind, "bounds": bounds}


def main():
    button = target("accessibility", [100, 100, 220, 144])
    label = target("text", [140, 110, 180, 132])  # the button's own text
    icon = target("visual", [96, 98, 150, 146])  # overlaps the button, IoU < 0.5
    neighbour = target("text", [320, 100, 420, 144])  # further along, no overlap
    edge = target("text", [200, 100, 300, 144])  # 20 px shared: separate controls
    merged = ocr_targets.merge([[button], [label, neighbour, edge], [icon]])
    assert merged == [button, neighbour, edge], merged
    print("PASS contained and overlapping boxes collapse into the earlier source")

    wide_text = target("text", [90, 90, 400, 150])  # a line that wraps the button
    assert ocr_targets.merge([[button], [wide_text]]) == [button]
    assert ocr_targets.MERGE_ORDER == ("accessibility", "text", "icons")
    print("PASS accessibility wins whichever box is larger; order is a11y, text, icons")

    snapshot = {"target_mode": "all"}
    assert ocr_targets.verifier(snapshot, button) == "accessibility"
    assert ocr_targets.verifier(snapshot, label) == "text"
    assert ocr_targets.verifier(snapshot, icon) == "text"
    assert ocr_targets.verifier({"target_mode": "icons"}, icon) == "icons"
    print("PASS merged targets verify with their own source's checks")

    # Dedupe is strictly across sources: nested controls of one source stay.
    tab = target("accessibility", [75, 10, 281, 44])
    close = target("accessibility", [251, 14, 277, 40])
    assert ocr_targets.merge([[tab, close]]) == [tab, close]
    entry = target("accessibility", [40, 200, 400, 240])
    reveal = target("accessibility", [360, 204, 396, 236])
    card_text = target("text", [60, 210, 200, 230])
    merged = ocr_targets.merge([[entry, reveal], [card_text]])
    assert merged == [entry, reveal], merged
    # IoU alone (0.5) drops a cross-source box that is only 67% contained.
    first, second = target("text", [0, 0, 90, 30]), target("visual", [30, 0, 120, 30])
    assert ocr_targets.merge([[first], [second]]) == [first]
    assert ocr_targets.merge([[first, second]]) == [first, second]
    print("PASS nested controls of one source stay; cross-source IoU still drops")

    check_sources()


def check_sources():
    """Optional sources are skipped with a reason; OCR failing is fatal."""
    real = ocr_targets.request, ocr_targets.ensure_worker, icon_detector.RUNTIME
    real_status = getattr(ocr_targets, "source_status", None)
    sent = []

    def fake_request(state, message, timeout=60, target_mode="text"):
        sent.append((target_mode, message))
        if message["action"] == "annotate":
            return {"size": [100, 100], "image_size": [100, 100]}
        failure = state["fail"].get(target_mode)
        if failure == "timeout":
            raise TimeoutError("timed out")
        if failure == "empty":
            return json.loads(b"")
        if failure == "subprocess":
            raise subprocess.CalledProcessError(1, ["systemctl"])
        return {
            "targets": [
                {
                    "kind": "visual" if target_mode == "icons" else target_mode,
                    "text": "x",
                    "bounds": [10, 10, 40, 40],
                    "center": [25, 25],
                }
            ]
        }

    def run(state):
        sent.clear()
        return ocr_targets.recognize_all(
            state, None, Path("source.png"), Path("out.png"), "a"
        )

    with tempfile.TemporaryDirectory() as directory:
        ocr_targets.request = fake_request
        ocr_targets.ensure_worker = lambda *_args, **_kwargs: None
        ocr_targets.source_status = lambda state: dict(state["unavailable"])
        try:
            base = {"directory": directory, "unavailable": {}}
            for failure in ("timeout", "empty", "subprocess"):
                result = run(
                    {**base, "fail": {"accessibility": failure, "icons": failure}}
                )
                assert result["sources"] == ["text"], (failure, result)
                assert set(result["skipped"]) == {"accessibility", "icons"}, result
            try:
                run({**base, "fail": {"text": "timeout"}})
            except TimeoutError:
                pass
            else:
                raise AssertionError("an OCR failure must be fatal")
            print("PASS optional sources that fail are skipped; OCR stays fatal")

            result = run(
                {**base, "fail": {}, "unavailable": {"icons": "icon runtime missing"}}
            )
            assert result["skipped"] == {"icons": "icon runtime missing"}, result
            assert not any(mode == "icons" for mode, _ in sent), sent
            annotate = next(m for _, m in sent if m["action"] == "annotate")
            assert all(set(t) == {"ref", "bounds"} for t in annotate["targets"])
            print("PASS unavailable sources are reported; annotate gets ref and bounds")
        finally:
            ocr_targets.request, ocr_targets.ensure_worker, icon_detector.RUNTIME = real
            ocr_targets.source_status = real_status

    reasons = ocr_targets.real_source_status(
        {"directory": "/nonexistent"}, icon_runtime=Path("/nonexistent")
    )
    assert "accessibility" in reasons["accessibility"], reasons
    assert "setup-icons" in reasons["icons"], reasons
    print("PASS missing accessibility and icon runtimes have readable reasons")


if __name__ == "__main__":
    main()
