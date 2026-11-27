---
name: computer-use
description: Operate native Linux apps and browsers on an isolated virtual desktop with its own mouse, keyboard, and clipboard. Use for computer driving or background GUI work without interrupting Alex's visible desktop.
---

# Computer use

## On Alex's machines

`computer-use` and `virtual-browser` are already on `PATH` on **arch** and
**forge**; skip setup there unless a command is missing. Neither machine runs a
window manager inside sessions (see below). From another machine, run the
commands through Today's `machine_exec` on arch or forge, or dispatch a chat there.

Today's own `computer_*` tools are deferred. Find them with `tool_search` using
`limit` 12 or more; the default of 8 hides some of them.

Forge got its packages on 2026-11-26 from the Arch Linux Archive snapshot of
2026-10-31, because its package database is that old and current mirrors 404:
`pacman --config` with `Server = https://archive.archlinux.org/repos/2026/10/31/$repo/os/$arch`
(no `-Sy`, no upgrade). That covers Xvfb, xdotool, xclip, zenity,
xdg-dbus-proxy, xauth, xdpyinfo, python-gobject, python-pillow, tk and tcl
(tkinter for the native checks), and `helium-browser-bin` 0.20.3.1-1 (installed
with `pacman -U` against the same archive). Forge also has the OCR and icon
runtimes (`setup-ocr`, `setup-icons`), and `~/.local/bin/computer-use` and
`~/.local/bin/virtual-browser` link to
`~/.agents/skills/computer-use/scripts/`. All native checks pass there. The
browser's CDP port, when enabled, listens on 127.0.0.1 only.

Forge has no desktop, and gtk4 (a zenity dependency) pulls in the desktop
portal, which host-bus apps such as Chromium would otherwise start (it then
fails with "cannot open display"). So the portal units are masked there
(Arch keeps its portal):
`systemctl --user mask xdg-desktop-portal.service xdg-desktop-portal-gtk.service xdg-document-portal.service xdg-permission-store.service`.
Sessions are unaffected either way: their filtered buses never reach it.

To roll Forge back:
`systemctl --user unmask xdg-desktop-portal.service xdg-desktop-portal-gtk.service xdg-document-portal.service xdg-permission-store.service`, then
`sudo pacman -Rns xorg-server-xvfb xdotool xclip zenity xdg-dbus-proxy xorg-xauth xorg-xdpyinfo python-gobject python-pillow helium-browser-bin tk`,
`rm ~/.local/bin/computer-use ~/.local/bin/virtual-browser`, and move
`~/.local/share/computer-use/{ocr-venv,icons-venv,models}` to dated trash.

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
computer-use start invoice-task --size 1440x1000 \
  --screenshot-directory /absolute/path/screenshots
computer-use launch invoice-task --id dialog -- zenity --entry --title='Invoice note'
computer-use windows invoice-task
```

Set `--screenshot-directory` when creating the session. `start`, `launch`, and `browser` return JSON with the new image's absolute path in `screenshot`. Every `input`, `exec`, `click`, and `clipboard set` returns a fresh image path on the final stdout line; `input`/`exec` keep their command output and exit code. View that image before choosing the next reference. Read-only `query`, `windows`, `status`, and `clipboard get` keep their existing output and references. Sessions started without the option require explicit screenshots.

`launch` starts a persistent process. `exec NAME -- COMMAND ...` runs a short command in the same display environment and returns its exit status. It is useful for inspection; use `launch` for apps so cleanup owns their processes. Launch arguments are passed directly, without a shell.

The helper forces X11 for common GTK, Qt, SDL, and Mozilla apps; Chromium/Electron may also require `--ozone-platform=x11`. This workflow supports X11-compatible apps. Native-Wayland-only apps require another compositor.

Check the app's process/profile behavior before launching it. Single-instance apps may forward requests through sockets or lock files to the visible process. Use the app's documented separate-instance/profile options and verify its window appears in `computer-use windows`. If it forwards to the host, stop and correct the launch; do not take over the existing window. Do not copy a whole application profile unless the task needs that data.

There is no window manager by default. `windowfocus`, `windowraise`, `windowmove`, and `windowsize` work through the input helper; `windowactivate` and workspace commands generally require a window manager. If needed, launch an installed X11 window manager as another owned app.

## Observe and interact

Accessibility annotations are the default. New desktops enable their private accessibility bus automatically, and the browser helper enables Chromium's native accessibility support. Run `computer-use setup-accessibility` to check the system dependencies; it does not install packages.

```bash
# View the screenshot returned by the last interaction, then choose its badge:
computer-use query invoice-task @a13
computer-use click invoice-task @a13
```

View the returned image and choose a letter-number badge from it. Screenshots print the image path; queries return the accessible name in `text`, its `role`, `kind: "accessibility"`, pixel bounds and center. Coordinates start at the original screenshot's top left. Only the focused window is annotated: use `windows` and `input -- windowfocus WINDOW_ID` when needed. Manually launched Chromium also needs `--force-renderer-accessibility=complete`.

`query` reads the current cached target. `click` rechecks focus, target semantics, bounds and pixels, then moves to the center and clicks through native X11 input. Inspect the screenshot before clicking: Chromium can return approximate hit-test results, so a badge does not prove a web control is unobscured. Hidden/disabled nodes, duplicate controls and centers obscured by higher native windows are excluded. Each read uses a fresh native process to avoid stale accessibility objects after navigation.

After each interaction, inspect its returned screenshot before using references again. Each annotated capture advances its letter (`a`, `b`, …, `g`, then `a`); only the current capture's references are accepted. Letters repeat after seven captures, so always choose from the latest image. Failed click verification also returns a fresh screenshot when possible. Take an explicit screenshot when the page changes after a capture or the session has no screenshot directory.

Automatic captures use accessibility annotations. An empty desktop or a session without accessibility returns a raw image. If annotation fails, the helper warns and returns a raw image without references; use an explicit text/icon capture as needed. Focus-change retries repeat only the capture. A screenshot does not establish that an asynchronous page load has finished. If capture itself fails after an interaction, inspect the session before repeating that interaction.

Add `--settle=MS` to `input`, `click`, `exec` or `repl` to wait, after the action and before any capture, until the screen has stayed unchanged for MS milliseconds (1 to 30000; a bare `--settle` means 500, but write it after the reference or as `--settle=MS`, since `click NAME --settle @a3` reads `@a3` as MS). The wait is capped at 5 s, or twice MS when that is longer. A change that starts later than MS after the action is missed, so pass a window longer than the delay you expect, for example `click NAME @a3 --settle=2500` for a page that loads in about two seconds. If the screen is still changing at the cap, the command warns and captures anyway. `stop` cancels a settle in progress. If settling itself fails, the action has already happened: the command warns (do not repeat the action), still captures, and keeps `exec`'s or the cell's exit status. `repl --settle` also returns one settled screenshot as a final `[image]` line, on its own line, even after a failed cell.

Small text (status bars, tab titles, badges) can be unreadable once a large screenshot is scaled down for viewing. Read it from a full-resolution crop:

```bash
computer-use zoom invoice-task 0 1560 640 1600   # x0 y0 x1 y1, exclusive end
```

`zoom` prints the path of a PNG cropped from a fresh capture, upscaled up to 8x when the crop is small (`--scale N` to choose, `--output PATH` to place it); output larger than 8192 px on its long edge is refused. It does not change references. Leave a few pixels of margin around the text you want to read.

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

These optional runtimes run locally on CPU. OCR returns recognized text boxes; the icon detector proposes visual regions, including some noninteractive content. Icon query results have `kind: "visual"` and `text: null`. Choose references from the image before querying or clicking. All modes share coordinates, the a–g sequence and invalidation; switching modes replaces the current snapshot. The next screenshot defaults to accessibility again.

When one source misses things (a canvas, a custom widget, an icon-only button with no accessible name), use the combined overlay:

```bash
computer-use screenshot invoice-task --targets all --output /absolute/path/all.png
```

`--targets all` merges accessibility, OCR text and icon targets into one badge set. Accessibility wins, then OCR fills gaps, then icons. A box that overlaps a kept one by IoU 0.5 or sits at least 80% inside it (a button's own label) is the same control and gets no second badge. Each target keeps its `kind` (`accessibility`, `text` or `visual`), and `click` verifies it the way its own mode would: focus and semantics for accessibility targets, pixels for the others. The snapshot lists the `sources` it used; accessibility or icons are skipped (and named under `skipped`) when unavailable, while OCR is required. It runs every detector, so it is slower than a single mode.

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

## Batch steps in the session REPL

For anything longer than one or two actions, prefer `repl`: one call runs a whole
Python cell inside the desktop, so loops, waits and multi-field forms cost one
turn instead of one per click. The REPL keeps its variables between calls until
`computer-use stop NAME` (or `repl NAME --reset`).

```bash
computer-use repl invoice-task <<'PY'
p = spawn("zenity", "--entry", "--title=Invoice note", capture=True)
focus(wait_window("Invoice note"))
type("Updated note")
key("Return")
print(p.communicate(timeout=10)[0])
display(screenshot())
PY
```

The cell comes from stdin or `--code` and runs in the command's working
directory, so relative paths mean what they would in your shell. The command
prints the cell's output (including output of subprocesses it runs), the value
of a trailing expression, and one `[image] PATH` line per image the cell passed
to `display()`; view those paths as you would a screenshot. Only output written
while the cell runs comes back: a background child (`cmd &`) that keeps writing
afterwards writes to that cell's file in `<session>/spawn-logs/cell-NNNN.out`.
Very long output is cut to fit the reply (`[output truncated ...]`). A failing cell
prints its traceback, exits 1 and keeps the REPL's state. Images go to the
session directory unless `--output-dir` is given; save anything you need to keep.

A cell holds the session's lock until it ends, and never outlives the command
that started it. `--timeout SECONDS` (default 300), `stop`, or SIGTERM/SIGINT to
the command interrupt the cell with `KeyboardInterrupt` and keep the REPL's
state; if the command itself is killed, the REPL interrupts the cell. A cell
that ignores the interrupt for 5 s is stopped with its REPL, and its state is
lost. Set `--timeout` below your own shell timeout. Whenever a REPL is replaced
(`--reset`, a crash, a stuck cell), stderr says `started a fresh REPL; previous
state lost (reason)`; a REPL that is still running a cell is never replaced. If
`stop` cannot take the session's lock within 10 s, it stops the REPL (which
ends anything the REPL started) and says so.
A cell returns only the images it displays: `--screenshot-directory` does not add
an automatic capture after `repl`, so end a cell with `display(screenshot())` when
you need to see the result.

Preloaded helpers:

- `screenshot(targets="accessibility" | "text" | "icons" | None)` returns the snapshot (`image`, `targets` with refs), like `query`. `find(snapshot, text=, role=, exact=True)` returns the one matching target or raises.
- `click("@a3" | target)` uses the same verification as `click`; `click(x, y, button=1, double=False)` clicks coordinates. `type(text)` types ASCII and pastes multiline or Unicode text through the session clipboard; `key("ctrl+s", ...)`, `scroll(dy, dx, x=, y=)`, `drag(x0, y0, x1, y1)`, `clipboard(text=None)`, `xdotool(...)`.
- `zoom((x0, y0, x1, y1), scale=None)` returns a full-resolution crop (upscaled when small) and `wait_stable(timeout=5, quiet=0.5)` waits until the screen has been unchanged for `quiet` seconds, returning False at `timeout` (which must be at least `quiet`); both share their code with the CLI's `zoom` and `--settle`. `a11y()` returns the AT-SPI tree as text; `ocr(image=None)` returns text boxes without replacing the current references.
- `windows()`, `wait_window(title)`, `focus(window)`, `spawn(*cmd, capture=False)` (a `Popen` that ends with the REPL; its output goes to the log file at `.log`, or to text pipes for `communicate()` with `capture=True`), `launch(app_id, *cmd)` (a session service, like `launch`), and `cdp` (the browser's DevTools port with `cdp.run(*agent_browser_args)`, which retires references like other input, or `None`).

References follow the CLI rules: every input helper retires them, and a click on a
stale or changed reference raises. The REPL runs as Alex inside the session's
environment, like `exec`; it adds no isolation boundary. Some apps (GTK4 zenity
without a window manager, for example) report broken accessibility bounds, so
the accessibility overlay can come back empty; use `targets="text"` there.

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
virtual-browser start research-task --no-cdp --url https://example.com \
  --screenshot-directory /absolute/path/screenshots
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

Choose a screenshot directory outside the temporary session directory to retain the images after cleanup. Stop cancels pending `input`/`exec` commands and terminates the recorded services, then removes the session files and login profiles. It leaves external screenshots and Alex's existing browser and apps in place. Verify the saved document or website result before claiming completion, and clean up task-owned desktops afterward.

Sessions whose services all died (a reboot, a crashed Xvfb, a chat that never
ran `stop`) stay in `computer-use list`. Clear them with `prune`:

```bash
computer-use prune --dry   # list what would move
computer-use prune
```

`prune` takes each session's lock before reading it, and keeps any session with
a unit that is not inactive or failed (including one still starting) or a
command holding its lock. It moves each dead session directory to
`~/.local/state/tmp-trash-YYYYMMDD/computer-use-NAME` (or `-2`, `-3`, … when that
name is taken; never replacing or deleting anything), runs
`systemctl --user reset-failed` on its units, and leaves external screenshot
directories alone. A session it cannot move is listed under `kept` with the
error, and the run continues.

`orphan_units` lists `computer-use-*` units that no session owns, each with its
`state`; inactive ones are left out. A unit belongs to a session when its
`state.json` records it, or when it is named `computer-use-<folder>-<6 hex>-…`
for a session folder that exists when the units are listed (so a session that
started during the run is skipped, and folder `a` never claims session `a-2`).
Units under `computer-use-check-` belong to the repository's checks and are
never reported. The real run resets failed orphans and lists them in
`reset_units`. If listing units fails, `orphan_units` is `null` and
`orphan_error` says why; the session report is still complete. A session with an
unusable `state.json` is kept with the reason. Before stopping an orphan
yourself, check what it is and that no new session folder now carries its name.

The helper uses Python 3, uv for the isolated OCR runtime, Xvfb, xauth, xdpyinfo, xdg-dbus-proxy, and user systemd. Screenshot/input/clipboard actions additionally use ImageMagick, xdotool, and xclip; the browser convenience uses helium-browser. Source: [scripts/computer_use.py](scripts/computer_use.py).
