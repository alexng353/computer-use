# Browser details

## Native accessibility workflow

When browser instrumentation is prohibited or the task calls for native accessibility, use:

```bash
virtual-browser start native-task --accessibility --no-cdp \
  --source-profile ~/.config/net.imput.helium --url https://example.com
computer-use screenshot native-task --targets accessibility --output /absolute/path/controls.png
computer-use query native-task @a13
computer-use click native-task @a13
```

`--no-cdp` starts the packaged Helium executable with no remote-debugging port and bypasses launcher-configured flags. Returned `cdp_port` is null; do not attach agent-browser. Use native input, screenshots and accessibility targets. Browser profile preferences still apply, and the helper copies only the login material described below. The private accessibility bus is owned and stopped with this desktop. Existing apps must be relaunched after enabling accessibility. Manually launched Chromium needs `--force-renderer-accessibility=complete` as well as the session's `--accessibility` environment.

## CDP workflow

`computer-use browser NAME` adds Helium to an existing desktop. It creates a separate profile, disables sync and extensions, forces X11, and exposes CDP on loopback only. Read the actual `cdp_port` from its output or `computer-use status NAME`; never assume a port or display number.

Omit `--source-profile` for a fresh browser without logins. For authorized access to Alex's signed-in Helium, use `--source-profile ~/.config/net.imput.helium`. `--profile` defaults to `Default`; inspect Local State when another profile is needed, without printing credentials.

The helper copies Local State and preferences and snapshots Cookies with SQLite backup, including committed WAL contents. It does not modify the source profile or copy saved passwords, browsing history, extensions, or sessions. The browser decrypts cookies using the existing unlocked keyring through the filtered bus. Login state is temporary and removed with the session.

Attach agent-browser once using `--session NAME --cdp PORT`, then use only `--session NAME` for later actions. Refresh refs after navigation or rerenders and verify field values before saving. In agent-browser 0.31.1, an immediate snapshot can precede a Google UI update, and iframe text waits may time out despite visible content. Inspect a fresh snapshot instead of repeating a mutation.

Use agent-browser screenshots for web content and `computer-use screenshot` for chrome and native dialogs. Prefer `agent-browser upload` when the file input is accessible.

For a temporary file input created by a cross-origin picker, CDP can intercept the chooser: enable `Page.setInterceptFileChooserDialog`, click the observed upload button in its frame's execution context, receive `Page.fileChooserOpened`, and pass its `backendNodeId` plus the authorized local file to `DOM.setFileInputFiles`. Disable interception in a `finally` block. Inspect the running browser's `/json/protocol` schema first. The photo-picker iframe may require its own Runtime execution context; agent-browser's `frame` selection did not reliably scope `eval` in the tested version. Use supported CDP rather than an application-internal upload endpoint.

Native file dialogs also work on the virtual display. Inspect a screenshot, focus the dialog, then use `computer-use input NAME -- key --clearmodifiers ctrl+l`, type the absolute path, and press Return. Verify that the site accepted the file rather than assuming a closed picker proves success.
