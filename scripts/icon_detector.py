"""Optional local CPU visual-target model and its explicitly invoked installer."""

import hashlib
import math
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

RUNTIME = Path.home() / ".local/share/computer-use/icons-venv"
MODEL = RUNTIME.parent / "models/omniparser-v2-icons.pt"
MODEL_URL = (
    "https://huggingface.co/microsoft/OmniParser-v2.0/resolve/"
    "6600256cb0f1b07651e3bc86166196307bad7e2d/icon_detect/model.pt"
)
MODEL_SHA256 = "dab3d4351ad00b035db829909a4db98354d5a90f6990e4ac00222a9a95d4bf57"


def valid_model(path):
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == MODEL_SHA256


def setup():
    scripts = Path(__file__).resolve().parent
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    if not (RUNTIME / "bin/python").exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(RUNTIME)], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            str(RUNTIME / "bin/python"),
            "--torch-backend",
            "cpu",
            str(scripts / "icon-requirements.lock"),
        ],
        check=True,
    )
    if not valid_model(MODEL):
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=MODEL.parent, delete=False) as temporary:
            pending = Path(temporary.name)
        try:
            with (
                urllib.request.urlopen(MODEL_URL, timeout=60) as response,
                pending.open("wb") as output,
            ):
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise RuntimeError(
                            "Icon model download exceeded its expected size"
                        )
                    output.write(chunk)
            if not valid_model(pending):
                raise RuntimeError(
                    "Icon model checksum mismatch; download was not installed"
                )
            pending.replace(MODEL)
        finally:
            pending.unlink(missing_ok=True)
    subprocess.run(
        [
            str(RUNTIME / "bin/python"),
            str(scripts / "ocr_worker.py"),
            "--warmup",
            "--targets",
            "icons",
        ],
        check=True,
    )


def area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )


def targets_from_detections(detections, size):
    width, height = size
    targets = []
    for box, confidence in detections:
        if (
            len(box) != 4
            or not all(math.isfinite(value) for value in (*box, confidence))
            or confidence < 0.15
        ):
            continue
        x1, y1, x2, y2 = box
        bounds = [
            max(0, math.floor(x1)),
            max(0, math.floor(y1)),
            min(width, math.ceil(x2)),
            min(height, math.ceil(y2)),
        ]
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        targets.append(
            {
                "kind": "visual",
                "text": None,
                "confidence": confidence,
                "bounds": bounds,
                "center": [
                    (bounds[0] + bounds[2] - 1) // 2,
                    (bounds[1] + bounds[3] - 1) // 2,
                ],
            }
        )
    # Keep specific controls instead of enclosing panels/cards that hide them.
    return [
        target
        for target in targets
        if not any(
            target is not other
            and area(target["bounds"]) > 1.5 * area(other["bounds"])
            and overlap(target["bounds"], other["bounds"]) / area(other["bounds"])
            > 0.85
            for other in targets
        )
    ]


def engine():
    # Importing the CLI or starting text OCR must not load the optional ML stack.
    if not valid_model(MODEL):
        raise RuntimeError(
            "Icon model missing or invalid; run computer-use setup-icons"
        )
    os.environ["YOLO_CONFIG_DIR"] = str(RUNTIME.parent / "icon-config")
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_VERBOSE"] = "false"
    from ultralytics import YOLO, settings

    settings.update({"sync": False})
    model = YOLO(str(MODEL))

    def detect(image):
        result = model.predict(
            image,
            imgsz=max(image.size),
            conf=0.15,
            iou=0.3,
            device="cpu",
            max_det=300,
            verbose=False,
        )[0]
        if result.boxes is None:
            return []
        return targets_from_detections(
            zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), strict=True),
            image.size,
        )

    return detect
