"""Check actual RapidOCR result variants and original-pixel verification."""

import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr.ch_ppocr_det.utils import TextDetOutput
from rapidocr.utils.output import RapidOCROutput

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ocr_worker import recognize, verify

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
