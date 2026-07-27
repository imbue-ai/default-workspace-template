First-boot deferred install now also installs `xvfb` and `xclip`, and a new `xvfb` supervised service provides the virtual display the browser fleet now runs headful under (the browser service is pointed at it via `DISPLAY=:99`). This is the infrastructure that enables the live browser's native clipboard copy/paste.

The workspace agent instructions (`CLAUDE.md`) now include a "Browser is available as a tool" section: use the `agentic-browser-fleet` skill for collaborative, human-shareable browsing, or Playwright directly for lightweight integration testing / scripting on the same Chromium.

The Fortress deferred install now also symlinks Fortress into Playwright's default browser-cache path, so a bare `playwright.chromium.launch()` (no `executable_path`) resolves to Fortress instead of erroring on the managed Chromium the Fortress swap intentionally stopped downloading. One engine, no second Chromium download.
