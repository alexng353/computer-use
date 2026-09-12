---
name: computer-use
description: Operate native Linux apps and browsers on an isolated virtual desktop with its own mouse, keyboard, and clipboard. Use for computer driving or background GUI work without interrupting Alex's visible desktop.
---

# Computer use

## First-use setup

Before starting the first desktop in a task, run the bundled setup script. Resolve
`<skill-directory>` from the location of this `SKILL.md`, including when installed
by `npx skills`; it is independent of the task's working directory.

```bash
python3 "<skill-directory>/scripts/setup.py"
```

Setup checks the Linux dependencies, links both commands into `~/.local/bin`, and
verifies a temporary desktop's screenshot, native input, clipboard, and cleanup.
Continue only after it exits successfully with `"ready": true`. Use the returned
absolute command paths, or apply its printed `PATH` export to subsequent commands.
Repeat setup after moving or updating the installed skill.

If setup reports an existing command conflict, preserve that command and rerun
with `--bin-dir ~/.local/share/computer-use/bin`; use the returned absolute paths
for this task. Setup can be rerun safely and never replaces another installation.
If dependencies are missing, identify the packages for the user's distribution
and request approval only for privileged system installation when it is not
already authorized. Rerun setup after installation. A missing user systemd manager
requires a Linux login session with user systemd and D-Bus; report that prerequisite
instead of attempting to operate the visible desktop. `--check` checks dependencies
without installing commands or starting a desktop; it does not verify desktop operation.

The browser convenience requires `helium-browser`. Install it only when browser
work needs that helper; native apps do not require a browser. OCR and icon runtimes
are optional: run their setup commands only when using those targeting modes.
Keep the skill directory intact because the helpers import sibling modules.

Use a separate Xvfb desktop for GUI work. Prefer an app's supported API or CLI when it can complete the task directly; use screenshots, accessibility targets and native input for app and browser UI. Use agent-browser/CDP when the task benefits from DOM access and permits browser instrumentation.

Use the `computer-use` command installed by setup. It creates owner-only session files under `~/.local/state/computer-use/<name>` and manages Xvfb, a filtered D-Bus proxy, and launched apps as separate user systemd services. `virtual-browser` is a shortcut for starting a desktop with Helium already attached.

This separates display, input, clipboard, and temporary browser profiles. Apps still share Alex's filesystem, network, and audio services. It does not authorize extra account changes, communications, or purchases.

## Start a desktop and launch apps

Choose a unique task name. Commands refuse to overwrite an existing session.

```bash
computer-use start invoice-task --size 1440x1000
computer-use launch invoice-task --id dialog -- zenity --entry --title='Invoice note'
computer-use windows invoice-task
computer-use screenshot invoice-task --output /absolute/path/desktop.png
```

`launch` starts a persistent process. `exec NAME -- COMMAND ...` runs a short command in the same display environment and returns its exit status. It is useful for inspection; use `launch` for apps so cleanup owns their processes. Launch arguments are passed directly, without a shell.

The helper forces X11 for common GTK, Qt, SDL, and Mozilla apps; Chromium/Electron may also require `--ozone-platform=x11`. This workflow supports X11-compatible apps. Native-Wayland-only apps require another compositor.

Check the app's process/profile behavior before launching it. Single-instance apps may forward requests through sockets or lock files to the visible process. Use the app's documented separate-instance/profile options and verify its window appears in `computer-use windows`. If it forwards to the host, stop and correct the launch; do not take over the existing window. Do not copy a whole application profile unless the task needs that data.

There is no window manager by default. `windowfocus`, `windowraise`, `windowmove`, and `windowsize` work through the input helper; `windowactivate` and workspace commands generally require a window manager. If needed, launch an installed X11 window manager as another owned app.

## Observe and interact

Accessibility annotations are the default. New desktops enable their private accessibility bus automatically, and the browser helper enables Chromium's native accessibility support. Run `computer-use setup-accessibility` to check the system dependencies; it does not install packages.

```bash
computer-use screenshot invoice-task --output /absolute/path/desktop.png
computer-use query invoice-task @a13
computer-use click invoice-task @a13
```

View the returned image and choose a letter-number badge from it. Screenshots print the image path; queries return the accessible name in `text`, its `role`, `kind: "accessibility"`, pixel bounds and center. Coordinates start at the original screenshot's top left. Only the focused window is annotated: use `windows` and `input -- windowfocus WINDOW_ID` when needed. Manually launched Chromium also needs `--force-renderer-accessibility=complete`.

`query` reads the current cached target. `click` rechecks focus, target semantics, bounds and pixels, then moves to the center and clicks through native X11 input. Inspect the screenshot before clicking: Chromium can return approximate hit-test results, so a badge does not prove a web control is unobscured. Hidden/disabled nodes, duplicate controls and centers obscured by higher native windows are excluded. Each read uses a fresh native process to avoid stale accessibility objects after navigation.

After clicking, typing, scrolling, launching an app, or another `input`/`exec` command, take a fresh screenshot before using references again. Each annotated capture advances its letter (`a`, `b`, …, `g`, then `a`); only the current capture's references are accepted. Letters repeat after seven captures, so always choose from the latest image. Failed click verification retires references and requires recapture.

On crowded screens, badges may use an added gutter on the right. The original screen stays at its original scale and top-left position. Gutter pixels are not desktop coordinates; use `query` or `click` with the badge reference to target its original control.

If an app exposes an incomplete tree, the reader exceeds its limit, or text content is easier to target, select a fallback explicitly:

```bash
# OCR text targets:
computer-use setup-ocr
computer-use screenshot invoice-task --targets text --output /absolute/path/text.png
# Visual icon/button candidates:
computer-use setup-icons
computer-use screenshot invoice-task --targets icons --output /absolute/path/icons.png
```

These optional runtimes run locally on CPU. OCR returns recognized text boxes; the icon detector proposes visual regions, including some noninteractive content. Icon query results have `kind: "visual"` and `text: null`. Choose references from the image before querying or clicking. There is no combined overlay. All modes share coordinates, the a–g sequence and invalidation; switching modes replaces the current snapshot. The next screenshot defaults to accessibility again.

For unannotated inspection, use `screenshot ... --raw`; this clears references and cannot be combined with `--targets`. If accessibility dependencies are unavailable, create the desktop with `start --no-accessibility` and use explicit text/icon targets or raw capture. For an older desktop created without the private bus, `launch --accessibility` or `browser --accessibility` enables it for that launch and future launches; already-running apps need relaunching.

```bash
computer-use input invoice-task -- mousemove 450 300 click 1
computer-use input invoice-task -- key --clearmodifiers ctrl+a
computer-use input invoice-task -- type --clearmodifiers --delay 1 'Updated note'
computer-use input invoice-task -- key --clearmodifiers Return
computer-use input invoice-task -- getmouselocation --shell
computer-use input invoice-task -- windowfocus WINDOW_ID
```

`input` forwards its arguments to xdotool with the correct DISPLAY and Xauthority. Do not run bare xdotool, host clipboard commands, or Hyprland focus/cursor commands for this desktop.

For multiline or Unicode text, use the virtual clipboard and paste:

```bash
computer-use clipboard invoice-task set < /absolute/path/text.txt
computer-use input invoice-task -- key --clearmodifiers ctrl+v
computer-use clipboard invoice-task get
```

The clipboard helper holds an X11 selection within this desktop. It never calls wl-copy or wl-paste. Screenshots capture only the virtual pixel buffer. Prefer an app's accessibility or document API when available; do not assume a successful click means the operation completed.

## Browsers

Read [references/browser.md](references/browser.md) for copied login state and browser setup. Prefer native accessibility targets and input, launching with `--no-cdp`. When a task needs DOM/CDP operations and permits browser instrumentation, read the agent-browser skill and `agent-browser skills get core` before attaching:

```bash
computer-use browser invoice-task --id browser \
  --source-profile ~/.config/net.imput.helium --url https://contacts.google.com
# Attach once with the returned cdp_port; keep the named session afterward.
agent-browser --session invoice-task --cdp PORT snapshot -i
agent-browser --session invoice-task click @e1
agent-browser --session invoice-task snapshot -i
```

For a browser-only task, the shortcut creates both desktop and browser:

```bash
virtual-browser start research-task --no-cdp --url https://example.com
```

Both commands use the same session registry. `computer-use status`, `screenshot`, and `stop` work with sessions created by either command.

## Live viewer

When the user wants to watch the virtual desktop, run `computer-use viewer NAME`.
If it reports missing `ffmpeg`, install the appropriate distribution package within
the user's authorization and retry. The viewer is optional for desktop control.
The command returns JSON with its local `url` after the first captured frame is
ready. Repeating the command for that desktop returns the same viewer.

Open the URL in the user's browser. In Codex, use `open_in_codex` with a browser
target and the returned URL when that tool is available. Otherwise provide the
URL for a browser on the same machine. This is the bundled local viewer; it needs
no private Codex integration. The viewer is read only and has a pause button.
Continue driving the desktop through the helper's input commands. The stream has
no target badges: use annotated screenshots to choose reference targets.

The server binds to `127.0.0.1`; keep it local. The viewer and its ffmpeg capture
are owned by the desktop and stop with `computer-use stop NAME`. Never reuse a
viewer's old URL for a newly created desktop; obtain its new URL from the helper.

## Isolation and cleanup

The filtered D-Bus proxy denies the host desktop portal and notification service while permitting existing keyrings. Without it, a native file picker can escape onto the physical desktop even when the app uses Xvfb. Keep the proxy in place. If the keyring requires unlocking, request the missing user interaction; do not drive a prompt on the visible desktop.

For isolation changes, compare the host focused-window address and cursor position before and after virtual input, check the host clipboard remains unchanged, and confirm a native dialog appears only in the session. A host screenshot is unnecessary for these checks. If a dialog escapes, close only the task's dialog and fix the connection before continuing.

```bash
computer-use list
computer-use status invoice-task
# Only if agent-browser was attached:
agent-browser --session invoice-task close
computer-use stop invoice-task
```

Save requested artifacts outside the session directory first. Stop cancels pending `input`/`exec` commands and terminates the recorded services, then removes the session files and login profiles. It leaves Alex's existing browser and apps running. Verify the saved document or website result before claiming completion, and clean up task-owned desktops afterward.

The helper uses Python 3, uv for the isolated OCR runtime, Xvfb, xauth, xdpyinfo, xdg-dbus-proxy, and user systemd. Screenshot/input/clipboard actions additionally use ImageMagick, xdotool, and xclip; the browser convenience uses helium-browser. Source: [scripts/computer_use.py](scripts/computer_use.py).
