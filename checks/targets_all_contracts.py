"""Merge rules for --targets all, without a desktop."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
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


if __name__ == "__main__":
    main()
