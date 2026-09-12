#!/usr/bin/env python3
"""Serve a computer-use desktop through its owned, isolated launch environment."""

import argparse
import json
import os
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

os.umask(0o077)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path, help="directory from computer-use status")
directory = parser.parse_args().directory
state = json.loads((directory / "state.json").read_text())
if (
    os.environ.get("DISPLAY") != state["display"]
    or os.environ.get("XAUTHORITY") != state["xauthority"]
):
    parser.error(
        "launch through computer-use for this session's display and Xauthority"
    )
width, height = map(int, state["size"].split("x"))
frame = directory / "viewer-frame.jpg"
PAGE = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live virtual desktop</title>
<style>
body{margin:0;background:#101319;color:#e5e7eb;font:13px system-ui}
header{padding:10px 12px;display:flex;justify-content:space-between;align-items:center}
small{color:#9ca3af}button{background:#29313f;border:0;border-radius:5px;padding:5px 9px;color:white}
#stage{position:relative;width:100%;line-height:0}img{width:100%;height:auto}
#cursor{position:absolute;width:24px;height:24px;border:3px solid #fde047;border-radius:50%;
transform:translate(-50%,-50%);box-shadow:0 0 0 3px #111827aa;pointer-events:none;display:none}
</style><header><span>● Live virtual desktop <small>· read only</small></span><button id="toggle">Pause</button></header>
<div id="stage"><img id="screen"><div id="cursor"></div></div>
<script>
let paused=false,timer;const screen=document.querySelector('#screen'),cursor=document.querySelector('#cursor');
function schedule(delay){clearTimeout(timer);if(!paused)timer=setTimeout(tick,delay)}
document.querySelector('#toggle').onclick=e=>{paused=!paused;e.target.textContent=paused?'Resume':'Pause';
clearTimeout(timer);if(!paused)tick()};
function tick(){if(paused)return;screen.src='/frame?t='+Date.now();
fetch('/cursor',{cache:'no-store'}).then(r=>r.json()).then(p=>{cursor.style.left=(100*p.x/p.width)+'%';
cursor.style.top=(100*p.y/p.height)+'%';cursor.style.display='block'}).catch(()=>{})}
screen.onload=()=>schedule(160);screen.onerror=()=>schedule(500);tick();
</script>""".encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if (
            self.headers.get("Sec-Fetch-Site") == "cross-site"
            or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
        ):
            self.send_error(403)
            return
        route = urlsplit(self.path).path
        try:
            if route == "/":
                body, kind = PAGE, "text/html; charset=utf-8"
            elif route == "/frame":
                if capture.poll() is not None:
                    self.send_error(503, "Desktop capture stopped")
                    return
                body, kind = frame.read_bytes(), "image/jpeg"
            elif route == "/cursor":
                result = subprocess.run(
                    ["xdotool", "getmouselocation", "--shell"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=2,
                )
                values = dict(line.split("=", 1) for line in result.stdout.splitlines())
                body = json.dumps(
                    {
                        "x": int(values["X"]),
                        "y": int(values["Y"]),
                        "width": width,
                        "height": height,
                    }
                ).encode()
                kind = "application/json"
            else:
                self.send_error(404)
                return
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
capture = subprocess.Popen(
    [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "x11grab",
        "-draw_mouse",
        "1",
        "-framerate",
        "6",
        "-video_size",
        state["size"],
        "-i",
        state["display"],
        "-q:v",
        "5",
        "-threads",
        "1",
        "-f",
        "image2",
        "-update",
        "1",
        "-atomic_writing",
        "1",
        str(frame),
    ]
)
(directory / "viewer-info.json").write_text(
    json.dumps({"url": f"http://127.0.0.1:{server.server_port}/"})
)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    server.serve_forever()
finally:
    capture.terminate()
    try:
        capture.wait(timeout=5)
    except subprocess.TimeoutExpired:
        capture.kill()
        capture.wait()
    server.server_close()
