The terminal reads the shell's windows through the shared `workspace_layout` library (moved out of `app_manifest`); its window sweep behaves as before.

- The window-closed hint is read through the shared `workspace_layout` model, so a post that is not the shell's hint (no window id or desktop) names no terminal.
