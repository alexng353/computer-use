"""Resident CPU-only OCR and analytical screenshot overlays; private Unix socket."""

import argparse
import json
import math
import os
import socket
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rapidocr import RapidOCR


def engine():
    return RapidOCR(
        params={
            "Global.use_cls": False,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )


def overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )


def annotate(image, targets, output):
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    except OSError:
        font = ImageFont.load_default(size=14)
    occupied = []
    text_bounds = [item["bounds"] for item in targets]
    for target in targets:
        x1, y1, x2, y2 = target["bounds"]
        label = target["ref"]
        width = math.ceil(draw.textlength(label, font=font)) + 8
        height = 20
        candidates = []
        for x, y in (
            (x2 + 2, y1 - height),
            (x1, y1 - height),
            (x2 + 2, y1),
            (x1, y2 + 2),
        ):
            x = max(0, min(x, image.width - width))
            y = max(0, min(y, image.height - height))
            candidate = (x, y, x + width, y + height)
            penalty = sum(overlap(candidate, box) for box in text_bounds)
            penalty += 3 * sum(overlap(candidate, box) for box in occupied)
            candidates.append((penalty, candidate))
        badge = min(candidates, key=lambda candidate: candidate[0])[1]
        occupied.append(badge)
        draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline="#34d399", width=2)
        draw.line(
            (x2 - 1, y1, badge[0], badge[1] + height / 2), fill="#34d399", width=1
        )
        draw.rectangle(badge, fill="#111827", outline="#fde047", width=1)
        draw.text((badge[0] + 4, badge[1] + 2), label, font=font, fill="#fde047")
    # Publish only a complete preview; screenshots retain the caller's file format.
    with tempfile.NamedTemporaryFile(
        dir=output.parent, suffix=output.suffix, delete=False
    ) as temporary:
        path = Path(temporary.name)
    try:
        image.save(path)
        path.replace(output)
    finally:
        path.unlink(missing_ok=True)


def recognize(ocr, message):
    with Image.open(message["source"]) as source:
        image = source.convert("RGB")
    output = ocr(image)
    targets = []
    if output.boxes is not None:
        for box, text, confidence in zip(output.boxes, output.txts, output.scores):
            if not text.strip() or not all(math.isfinite(float(v)) for v in box.flat):
                continue
            left, top = box.min(axis=0)
            right, bottom = box.max(axis=0)
            x1, y1 = max(0, math.floor(left)), max(0, math.floor(top))
            x2, y2 = (
                min(image.width, math.ceil(right)),
                min(image.height, math.ceil(bottom)),
            )
            if x2 <= x1 or y2 <= y1:
                continue
            targets.append(
                {
                    "text": text,
                    "confidence": float(confidence),
                    "bounds": [x1, y1, x2, y2],
                    "center": [(x1 + x2 - 1) // 2, (y1 + y2 - 1) // 2],
                }
            )
    targets.sort(key=lambda item: (item["bounds"][1], item["bounds"][0]))
    for index, target in enumerate(targets, start=1):
        target["ref"] = message["prefix"] + str(index)
    annotate(image, targets, Path(message["output"]))
    return {"size": list(image.size), "targets": targets}


def verify(message):
    with (
        Image.open(message["source"]) as source,
        Image.open(message["current"]) as current,
    ):
        matches = source.size == current.size
        if matches:
            bounds = tuple(message["bounds"])
            matches = (
                source.convert("RGB").crop(bounds).tobytes()
                == current.convert("RGB").crop(bounds).tobytes()
            )
    return {"matches": matches}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session-dir", type=Path)
    mode.add_argument("--warmup", action="store_true")
    args = parser.parse_args()
    ocr = engine()
    if args.warmup:
        return
    path = args.session_dir / "ocr.sock"
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(4)
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(60)
                try:
                    with connection.makefile("rb") as incoming:
                        message = json.loads(incoming.readline(65536))
                    action = message.get("action")
                    if action == "ping":
                        result = {"ready": True}
                    elif action == "recognize":
                        result = recognize(ocr, message)
                    elif action == "verify":
                        result = verify(message)
                    else:
                        raise ValueError("Unknown OCR request")
                except Exception as exc:  # noqa: BLE001 - A bad image must not kill the resident worker.
                    result = {"error": str(exc)}
                try:
                    connection.sendall(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass


if __name__ == "__main__":
    main()
