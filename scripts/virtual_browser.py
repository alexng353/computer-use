#!/usr/bin/env python3
"""Convenience browser launcher backed by computer-use."""

import json
import subprocess
import sys
from pathlib import Path

HELPER = str(Path(__file__).resolve().with_name("computer_use.py"))


def call(*args, **kwargs):
    return subprocess.run([sys.executable, HELPER, *args], check=True, **kwargs)


if len(sys.argv) < 3 or sys.argv[1] not in ["start", "status", "stop", "screenshot"]:
    print(
        "Usage: virtual-browser {start|status|stop|screenshot} NAME [options]\n"
        "start accepts computer-use browser options; use computer-use --help for desktop apps."
    )
    raise SystemExit(0 if "--help" in sys.argv else 2)
action, name, *options = sys.argv[1:]
try:
    if action == "start":
        result = call("start", name, capture_output=True, text=True)
        try:
            result = call("browser", name, *options, capture_output=True, text=True)
            app = json.loads(result.stdout)
            result = call("status", name, capture_output=True, text=True)
            state = json.loads(result.stdout)
            state.update(app)
            print(json.dumps(state, indent=2))
        except Exception:
            call("stop", name, stdout=subprocess.DEVNULL)
            raise
    else:
        call(action, name, *options)
except subprocess.CalledProcessError as exc:
    if exc.stderr:
        print(exc.stderr, file=sys.stderr, end="")
    raise SystemExit(exc.returncode)
