---
name: computer-use
description: Operate native Linux apps and browsers on an isolated virtual desktop with its own mouse, keyboard, and clipboard. Use for computer driving or background GUI work without interrupting Alex's visible desktop.
---

# Computer use

Use a separate Xvfb desktop for GUI work. Prefer an app's supported API or CLI when it can complete the task directly; use screenshots and native input for desktop UI, and agent-browser/CDP for browser content.

The global `computer-use` command is on PATH. It creates owner-only session files under `~/.local/state/computer-use/<name>` and manages Xvfb, a filtered D-Bus proxy, and launched apps as separate user systemd services. `virtual-browser` is a shortcut for starting a desktop with Helium already attached.

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

Run `computer-use setup-ocr` once to install the pinned RapidOCR CPU runtime and prepare its models. Annotated screenshots are the default. View the returned image with the image-viewing tool; the command prints its path, not the OCR text list. A resident worker stays loaded for each desktop and stops with the session.

```bash
computer-use screenshot invoice-task --output /absolute/path/desktop.png
computer-use query invoice-task @a13
computer-use click invoice-task @a13
```

Read the letter-number badge on the current image before choosing a reference. `query` returns cached text, pixel bounds, and center, measured from the screenshot's top left; recapture first if the app may have changed. Use the coordinates as anchors when a nearby icon has no text box. `click` verifies that the target pixels still match, moves to the center, and left-clicks through native X11 input.

After clicking, typing, scrolling, launching an app, or another `input`/`exec` command, take a fresh screenshot before using references again. Each annotated capture advances its letter (`a`, `b`, …, `g`, then `a`); only the current capture's references are accepted. Letters repeat after seven captures, so always choose from the latest image. A background redraw within the target box also rejects a click and requires recapture.

For unannotated inspection, use `screenshot ... --raw`; this clears current references. For icons and other uncovered targets, inspect the current screenshot and use native coordinates below. Window IDs come from the session's window list.

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

For browser work, read the agent-browser skill and `agent-browser skills get core`. Read [references/browser.md](references/browser.md) for copied login state, CDP connection, and file-upload details.

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
virtual-browser start research-task --url https://example.com
```

Both commands use the same session registry. `computer-use status`, `screenshot`, and `stop` work with sessions created by either command.

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
