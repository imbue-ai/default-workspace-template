The browser app follows phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- The browser's instances API, its shell nudge, and the fleet's instances adapter are deleted; a browser session is a window at `/?session=<name>`, and the fleet pulls a session into the desktop through `layout.py open browser --path "/?session=<name>"`.

- Session records stay in `data/.apps/browser/instances.json`; a start URL must be an absolute `http(s)` URL (`InvalidStartUrlError` otherwise).

- The manifest declares launch paths only; the retired `instances`, `instances_url`, and `actions` keys are gone.

- One browser: the fleet's cap defaults to 1, and `/new` (and a nameless `POST /browsers`) answers that browser -- created once, started again when it was stopped, a `url` opened as a new tab in front -- rather than minting another (the launch path is labelled "Open Browser"). The browser lives as long as a window shows it: the daemon sweeps the shell's desktops every 90 seconds and at once when the shell posts a closed window to `POST /api/window-closed`, and stops a browser some window showed (`window_seen` in the manifest) once none shows it; the profile is never deleted by a close. A workspace saved with more browsers than the cap restores the rest stopped. The fleet CLI opens the browser's window minimized.
