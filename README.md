# computer-use

Native X11 desktop automation on a private virtual display, with local RapidOCR CPU text targets. Screenshots show letter-number badges; commands can query their coordinates or click their centers without browser instrumentation.

| Raw screenshot | OCR targets |
|---|---|
| ![Raw native window](docs/images/raw.png) | ![Letter-number targets](docs/images/targets.png) |

## Setup

Requires Linux, user systemd, Python 3, uv, Xvfb, xauth, xdpyinfo, xdg-dbus-proxy, ImageMagick, and xdotool. Clipboard commands additionally use xclip; the browser convenience uses helium-browser.

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/scripts/computer_use.py" ~/.local/bin/computer-use
ln -s "$PWD/scripts/virtual_browser.py" ~/.local/bin/virtual-browser
computer-use setup-ocr
```

Keep the entire repository directory together: the entry points import sibling modules. For an existing installation, update its directory link or relink both commands to this checkout; copying only the entry-point file is unsupported. `setup-ocr` creates a separate Python 3.12 environment under `~/.local/share/computer-use/ocr-venv` and installs the complete dependency lock and prepares the models selected by the pinned RapidOCR release. Setup can download packages and model weights. Screenshot inference runs locally on CPU; images are never sent to an OCR service. Each desktop starts its own resident worker lazily and owns its cleanup.

## Capture, locate, click

```bash
computer-use start demo --size 1440x1000
computer-use launch demo --id app -- your-x11-app
computer-use screenshot demo --output /tmp/screen.png
# View the image, then choose a badge:
computer-use query demo @a13
computer-use click demo @a13
computer-use screenshot demo --output /tmp/after.png
computer-use stop demo
```

Screenshots print only the image path. The recognized text and geometry stay in the owner-only session directory. `query` prints one target as JSON: `ref`, `text`, `confidence`, `bounds`, `center`, and source-image information. Bounds are `[x1, y1, x2, y2]` with exclusive right/bottom edges; center is `[x, y]`, all in original screenshot pixels from the top left. These are OCR text bounds, not whole-control or accessibility bounds.

References advance through `@a1`, `@b1`, …, `@g1`, then wrap. A new capture replaces the reference map; all `input`, `exec`, launch/browser, clipboard-set, and click operations invalidate it. Queries do not invalidate it and return cached coordinates; they do not recheck live pixels. Use a fresh screenshot when the app may have changed. A click checks the current pixels within its text box against the captured pixels before sending native input. A changed target rejects the click. Repeated letters are intentionally short visual hints, not globally unique capture IDs: always use the latest image, including after wraparound. Pixels alone cannot distinguish two logically different controls with identical appearance.

For unannotated images or targets that OCR misses:

```bash
computer-use screenshot demo --raw --output /tmp/raw.png
computer-use input demo -- mousemove 450 300 click 1
```

Raw capture clears references and does not require OCR. The existing input, clipboard, window, and browser commands remain available. See [SKILL.md](SKILL.md) for the complete desktop workflow and isolation boundaries.

## Development

```bash
python -m py_compile scripts/*.py checks/*.py
ruff check scripts checks
ruff format --check scripts checks
~/.local/share/computer-use/ocr-venv/bin/python checks/ocr_contracts.py
python checks/native_ocr.py
```

No GPU or cloud OCR runtime is required. The worker uses RapidOCR's detection/recognition boxes directly, clips them to the image, assigns labels in top-to-bottom/left-to-right order, and renders readable badges. Session input and screenshot publication are serialized with a persistent per-name file lock. `stop` requests cancellation before waiting for that lock, and waiting commands verify the session's unique identity after acquiring it. Blocked `input`/`exec` subprocess groups are terminated when the session stops. Lock files under the private `.locks` directory survive session deletion so queued commands cannot bypass a replacement session's lock.

The native check additionally requires tkinter and an active user systemd bus. It creates uniquely named Xvfb desktops and removes them on completion. `python checks/native_ocr.py --output /tmp/ocr-check` retains artifacts, including `initial-raw.png` and `initial-targets.png`, which reproduce the README preview. The contract check exercises the SDK's detection-only return type and pixel verification without a desktop.

To update the resolved dependency set, run `uv pip compile scripts/ocr-requirements.txt --python-version 3.12 --no-header --no-annotate -o scripts/ocr-requirements.lock`, then run setup and both checks.
