The root uv workspace excludes `system/apps/terminal_pty`, a manifest-only app directory whose program is an entry point of `system/apps/terminal`.

The workspace app model contract (`docs/system/blueprint/workspace-app-model/contracts.md`) describes the terminal as it now runs: the instance URL is the wrapper page `/?session=<key>&tab={tab}`, the built-in manifests table lists `terminal-pty`, and the `ttyd-focus` note says the wrapper passes the message on to the pty frame.
