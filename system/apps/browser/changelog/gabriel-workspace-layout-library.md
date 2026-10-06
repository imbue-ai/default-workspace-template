`agentic-browser-fleet` opens a browser's viewer window through the shared `workspace_layout` library's client instead of running `system/scripts/layout.py`, and the daemon reads the shell's windows through the same library; what the user sees is unchanged.

- The window-closed hint is read through the shared `workspace_layout` model, so a post that is not the shell's hint (no window id or desktop) names no browser.
