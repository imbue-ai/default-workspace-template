The terminal app follows phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- The terminal wrapper's instances API, the tmux hooks that fed it, the shell nudge, and the per-tab session tracking are deleted; a terminal is a window at `/?session=<name>`, and the wrapper's `/api/health` is what the update apply probes after a restart.

- The manifest declares launch paths only; the retired `instances`, `instances_url`, and `actions` keys are gone.

- `bin/notify_terminal_session.py` stays as a quiet no-op: a tmux server started on an earlier release keeps calling it from its session hooks until the container restarts (a `CLEANUP:` note says when it can go).
