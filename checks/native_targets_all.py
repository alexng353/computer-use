"""--targets all merges accessibility, OCR and icon targets into one badge set.

A Helium page (no CDP) has a text link, an icon-only button and a label drawn
on a canvas. Each source alone misses or doubles some of them; the merged set
gives each exactly one badge (accessibility first), and each badge clicks.
Requires Helium, setup-ocr and setup-icons.
"""

import json
import os
import secrets
import tempfile
from pathlib import Path

from native_ocr import STATE, command, wait_until

HTML = """<!doctype html><title>Targets all</title>
<style>body{background:#fff;color:#111;font:24px sans-serif;margin:40px}
a{color:#1d4ed8} #settings{width:72px;height:72px;border:none;border-radius:14px;
background:#e5e7eb;margin:30px 0;display:block}
canvas{display:block}</style>
<h1 id="status">Ready</h1>
<p><a href="#guide" onclick="say('Guide opened')">Read the guide</a></p>
<button id="settings" aria-label="Settings" onclick="say('Settings opened')">
<svg width="40" height="40" viewBox="0 0 24 24" aria-hidden="true"><path fill="#374151"
d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm9 4-2.2-.7.4-2.3-2-1.2-1.6 1.7L13.4 7 12 5l-1.4 2
-2.2.5-1.6-1.7-2 1.2.4 2.3L3 12l2.2.7-.4 2.3 2 1.2 1.6-1.7 2.2.5L12 19l1.4-2 2.2-.5
1.6 1.7 2-1.2-.4-2.3z"/></svg></button>
<canvas id="board" width="420" height="90"></canvas>
<script>
function say(text){document.getElementById('status').textContent=text}
const board=document.getElementById('board'),pen=board.getContext('2d');
pen.fillStyle='#fef3c7';pen.fillRect(0,0,420,90);
pen.fillStyle='#111';pen.font='28px sans-serif';pen.fillText('Canvas label',20,55);
board.onclick=()=>say('Canvas clicked');
</script>
"""


def snapshot(name, directory, mode):
    command(
        "screenshot",
        name,
        "--targets",
        mode,
        "--output",
        str(directory / f"{mode}.png"),
        timeout=120,
    )
    return json.loads((STATE / name / "ocr-snapshot.json").read_text())


def inside(target, bounds, margin=4):
    x, y = target["center"]
    x1, y1, x2, y2 = bounds
    return x1 - margin <= x < x2 + margin and y1 - margin <= y < y2 + margin


def one(targets, predicate, label):
    found = [t for t in targets if predicate(t)]
    assert len(found) == 1, (label, found)
    return found[0]


def status_text(name, directory):
    return " ".join(t["text"] for t in snapshot(name, directory, "text")["targets"])


def main(directory):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    name = "check-all-" + secrets.token_hex(4)
    page = directory / "page.html"
    page.write_text(HTML)
    try:
        command("start", name, "--size", "1000x760")
        command("browser", name, "--no-cdp", "--url", page.as_uri(), timeout=60)
        wait_until(
            lambda: any(
                "Targets all" in w["title"]
                for w in json.loads(command("windows", name).stdout)
            ),
            "browser window",
        )
        window = next(
            w
            for w in json.loads(command("windows", name).stdout)
            if "Targets all" in w["title"]
        )
        command("input", name, "--", "windowfocus", str(window["id"]))
        command("repl", name, "--code", "wait_stable(timeout=10, quiet=1)")

        accessible = snapshot(name, directory, "accessibility")["targets"]
        link = one(
            accessible,
            lambda t: t.get("role") == "link" and t["text"] == "Read the guide",
            "accessible link",
        )
        button = one(
            accessible,
            lambda t: t.get("role") == "button" and t["text"] == "Settings",
            "accessible icon button",
        )
        assert not any("Canvas" in (t["text"] or "") for t in accessible)
        text = snapshot(name, directory, "text")["targets"]
        label = one(text, lambda t: t["text"] == "Canvas label", "OCR canvas label")
        # The sources overlap, so the merge has duplicates to drop.
        assert any(inside(t, link["bounds"]) for t in text), "OCR misses the link"
        icons = snapshot(name, directory, "icons")["targets"]
        print(
            "INFO icons overlapping the button:",
            sum(inside(t, button["bounds"]) for t in icons),
            flush=True,
        )

        merged = snapshot(name, directory, "all")
        assert merged["target_mode"] == "all", merged["target_mode"]
        assert set(merged["sources"]) == {"accessibility", "text", "icons"}, merged
        targets = merged["targets"]
        for label_name, bounds, kind in (
            ("link", link["bounds"], "accessibility"),
            ("icon button", button["bounds"], "accessibility"),
            ("canvas label", label["bounds"], "text"),
        ):
            badge = one(targets, lambda t, b=bounds: inside(t, b), label_name)
            assert badge["kind"] == kind, (label_name, badge)
        refs = [t["ref"] for t in targets]
        assert len(set(refs)) == len(refs) and all(
            ref[0] == refs[0][0] for ref in refs
        ), refs
        print("PASS one badge each for a link, an icon button and a canvas label")

        for label_name, bounds, expected in (
            ("link", link["bounds"], "Guide opened"),
            ("icon button", button["bounds"], "Settings opened"),
            ("canvas label", label["bounds"], "Canvas clicked"),
        ):
            targets = snapshot(name, directory, "all")["targets"]
            badge = one(targets, lambda t, b=bounds: inside(t, b), label_name)
            command("query", name, "@" + badge["ref"])
            command("click", name, "@" + badge["ref"], timeout=60)
            command("repl", name, "--code", "wait_stable(timeout=5, quiet=0.5)")
            seen = status_text(name, directory)
            assert expected in seen, (label_name, seen)
            command("query", name, "@" + badge["ref"], ok=False)
        print("PASS each merged badge clicks with its own verification")
    finally:
        if (STATE / name).exists():
            command("stop", name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="computer-use-all-check-") as temporary:
        main(Path(temporary))
