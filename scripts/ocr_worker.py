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


def badge_layout(size, targets, widths):
    image_width, image_height = size
    height, gap = 20, 4
    # A pixel mask keeps dense screens from scanning every obstacle per candidate.
    occupied = Image.new("1", size)
    occupancy = ImageDraw.Draw(occupied)
    for target in targets:
        x1, y1, x2, y2 = target["bounds"]
        occupancy.rectangle((x1 - 2, y1 - 2, x2 + 1, y2 + 1), fill=1)
    badges = []
    overflow = 0
    rows = max(1, (image_height - 16) // (height + gap))
    column_width = max(widths, default=0) + 16
    for target, width in zip(targets, widths, strict=True):
        x1, y1, x2, y2 = target["bounds"]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        anchors = [
            (x, y)
            for x in (x1, cx - width // 2, x2 - width)
            for y in (y1 - height - gap, y2 + gap)
        ] + [(x1 - width - gap, cy - height // 2), (x2 + gap, cy - height // 2)]
        candidates = set()
        if width <= image_width and height <= image_height:
            for offset in range(0, 65, 8):
                for dx, dy in ((offset, 0), (-offset, 0), (0, offset), (0, -offset)):
                    for x, y in anchors:
                        x = max(0, min(x + dx, image_width - width))
                        y = max(0, min(y + dy, image_height - height))
                        candidates.add((x, y, x + width, y + height))

        def distance(box, bounds=target["bounds"]):
            x1, y1, x2, y2 = bounds
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            horizontal = max(x1 - box[2], box[0] - x2, 0)
            vertical = max(y1 - box[3], box[1] - y2, 0)
            return (
                horizontal**2 + vertical**2,
                ((box[0] + box[2]) / 2 - cx) ** 2 + ((box[1] + box[3]) / 2 - cy) ** 2,
                box,
            )

        badge = next(
            (
                box
                for box in sorted(candidates, key=distance)
                if occupied.crop(box).getbbox() is None
            ),
            None,
        )
        if badge is None:
            # Dense screens still need every reference, without covering the UI.
            column, row = divmod(overflow, rows)
            x, y = image_width + 8 + column * column_width, 8 + row * (height + gap)
            badge = (x, y, x + width, y + height)
            overflow += 1
        badges.append(badge)
        left, top, right, bottom = badge
        occupancy.rectangle((left - 2, top - 2, right + 1, bottom + 1), fill=1)
    columns = math.ceil(overflow / rows)
    return badges, (
        image_width + columns * column_width,
        max(image_height, height + 16) if overflow else image_height,
    )


def annotate(image, targets, output):
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    except OSError:
        font = ImageFont.load_default(size=14)
    measuring = ImageDraw.Draw(image)
    widths = [
        math.ceil(measuring.textlength(item["ref"], font=font)) + 8 for item in targets
    ]
    badges, size = badge_layout(image.size, targets, widths)
    canvas = Image.new("RGB", size, "#111827")
    canvas.paste(image)
    draw = ImageDraw.Draw(canvas)
    for target, badge in zip(targets, badges, strict=True):
        x1, y1, x2, y2 = target["bounds"]
        draw.rectangle((x1 - 2, y1 - 2, x2 + 1, y2 + 1), outline="#34d399", width=1)
        draw.line(
            (
                (x1 + x2) // 2,
                (y1 + y2) // 2,
                (badge[0] + badge[2]) // 2,
                (badge[1] + badge[3]) // 2,
            ),
            fill="#34d399",
            width=1,
        )
    # Connectors and neighbouring outlines must not alter any detected text.
    for target in targets:
        bounds = target["bounds"]
        canvas.paste(image.crop(bounds), bounds[:2])
    for target, badge in zip(targets, badges, strict=True):
        left, top, right, bottom = badge
        draw.rectangle(
            (left, top, right - 1, bottom - 1),
            fill="#111827",
            outline="#fde047",
            width=1,
        )
        draw.text((left + 4, top + 2), target["ref"], font=font, fill="#fde047")
    # Publish only a complete preview; screenshots retain the caller's file format.
    with tempfile.NamedTemporaryFile(
        dir=output.parent, suffix=output.suffix, delete=False
    ) as temporary:
        path = Path(temporary.name)
    try:
        canvas.save(path)
        path.replace(output)
    finally:
        path.unlink(missing_ok=True)
    return canvas.size


def recognize(ocr, message):
    with Image.open(message["source"]) as source:
        image = source.convert("RGB")
    output = ocr(image)
    targets = []
    # Recognition failure can return detection-only output with no txts attribute.
    boxes, texts, scores = (
        getattr(output, key, None) for key in ("boxes", "txts", "scores")
    )
    if all(value is not None for value in (boxes, texts, scores)):
        for box, text, confidence in zip(boxes, texts, scores, strict=True):
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
    rendered_size = annotate(image, targets, Path(message["output"]))
    return {
        "size": list(image.size),
        "image_size": list(rendered_size),
        "targets": targets,
    }


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
