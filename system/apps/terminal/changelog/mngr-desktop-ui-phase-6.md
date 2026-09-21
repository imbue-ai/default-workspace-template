The terminal app follows phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- The terminal wrapper's instances API, the tmux hooks that fed it, the shell nudge, and the per-tab session tracking are deleted; a terminal is a window at `/?session=<name>`, and the wrapper's `/api/health` is what the update apply probes after a restart.

- The manifest declares launch paths only; the retired `instances`, `instances_url`, and `actions` keys are gone.

- `bin/notify_terminal_session.py` stays as a quiet no-op: a tmux server started on an earlier release keeps calling it from its session hooks until the container restarts (a `CLEANUP:` note says when it can go).

- A terminal lives as long as a window shows it: the app marks a remembered terminal once a desktop window shows it (`is_window_seen` in the store) and deletes it once no window does, on a 90 second sweep of the shell's desktops and at once when the shell posts a closed window to `POST /api/window-closed` (the manifest's `window_closed_path`). A terminal no window ever showed is never collected. The default shortcut is in new mode.

- The shell's close hint now marks the terminal the closed window showed as window-seen before the sweep it brings, so a terminal whose window opened and closed between two 90 second sweeps (which no sweep ever observed) is collected too, instead of surviving forever.
