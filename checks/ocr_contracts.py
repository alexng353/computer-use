"""Check actual RapidOCR result variants and original-pixel verification."""

import sys
import tempfile
from itertools import combinations
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr.ch_ppocr_det.utils import TextDetOutput
from rapidocr.utils.output import RapidOCROutput

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ocr_worker import annotate, badge_layout, overlap, recognize, verify

with tempfile.TemporaryDirectory(prefix="computer-use-contract-") as temporary:
    root = Path(temporary)
    source, output, current = (
        root / name for name in ("source.png", "output.png", "current.png")
    )
    Image.new("RGB", (200, 100), "white").save(source)
    boxes = np.array([[[10, 10], [80, 10], [80, 30], [10, 30]]], dtype=np.float32)
    partial = TextDetOutput(boxes=boxes, scores=np.array([0.99]))
    request = {"source": str(source), "output": str(output), "prefix": "a"}
    result = recognize(lambda _: partial, request)
    assert result["targets"] == []
    with Image.open(source) as original, Image.open(output) as rendered:
        assert original.tobytes() == rendered.tobytes()
    print("PASS detection-only SDK result publishes image with zero targets")

    complete = RapidOCROutput(boxes=boxes, txts=("Save",), scores=(0.99,))
    result = recognize(lambda _: complete, request)
    assert result["targets"][0]["ref"] == "a1"
    assert result["targets"][0]["bounds"] == [10, 10, 80, 30]
    with Image.open(source) as image:
        image.save(current)
        probe = {
            "source": str(source),
            "current": str(current),
            "bounds": [10, 10, 80, 30],
        }
        assert verify(probe)["matches"]
        image.putpixel((20, 20), (0, 0, 0))
        image.save(current)
        assert not verify(probe)["matches"]
        image.resize((300, 100)).save(current)
        assert not verify(probe)["matches"]
    print("PASS complete SDK result, exact pixel comparison and resize rejection")

    dense = Image.new("RGB", (200, 100), "#bacdef")
    targets = [
        {"ref": f"a{index + 1}", "bounds": [0, y, 200, y + 14]}
        for index, y in enumerate(range(0, 96, 16))
    ]
    annotate(dense.copy(), targets, output)
    with Image.open(output) as rendered:
        for target in targets:
            bounds = target["bounds"]
            assert rendered.crop(bounds).tobytes() == dense.crop(bounds).tobytes(), (
                "Annotation obscured detected text",
                target,
            )
        assert rendered.width > dense.width, "Crowded labels need a separate gutter"
    print("PASS dense annotations preserve every detected text pixel")

    for size, bounds in (
        ((200, 100), [target["bounds"] for target in targets]),
        ((10, 10), [[0, 0, 10, 10]] * 8),
        ((200, 100), [[20, 40, 80, 60]]),
        ((200, 100), []),
    ):
        cases = [{"bounds": box} for box in bounds]
        badges, canvas_size = badge_layout(size, cases, [30] * len(cases))
        assert len(badges) == len(cases)
        for badge in badges:
            assert 0 <= badge[0] < badge[2] <= canvas_size[0]
            assert 0 <= badge[1] < badge[3] <= canvas_size[1]
            assert not any(overlap(badge, box) for box in bounds)
        assert not any(overlap(a, b) for a, b in combinations(badges, 2))
        if len(cases) <= 1:
            assert canvas_size == size, "Uncrowded images should keep their size"
    print("PASS badge separation, edge clipping, tiny images and gutter overflow")

    dense.save(source)
    dense_boxes = np.array(
        [
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            for x1, y1, x2, y2 in (target["bounds"] for target in targets)
        ],
        dtype=np.float32,
    )
    complete = RapidOCROutput(
        boxes=dense_boxes,
        txts=("Crowded",) * len(targets),
        scores=(0.99,) * len(targets),
    )
    result = recognize(lambda _: complete, request)
    assert result["size"] == [200, 100]
    assert [item["bounds"] for item in result["targets"]] == [
        item["bounds"] for item in targets
    ]
    with Image.open(output) as rendered:
        assert rendered.width > result["size"][0]
    print("PASS gutter leaves original target coordinates and source size intact")
