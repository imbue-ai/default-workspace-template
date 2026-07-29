Added a `system_interface_layout_dir` setting to `Config` (so
`SYSTEM_INTERFACE_LAYOUT_DIR`, like every other `SYSTEM_INTERFACE_*` setting),
consulted by `_primary_agent_layout_dir()`. When set, it wins over the usual
`$MNGR_HOST_DIR/agents/<MNGR_AGENT_ID>/workspace_layout/` path (even when a real
`MNGR_AGENT_ID` is present). The new `update-system-interface` live-editing
preview points this at a throwaway copy of the live layout, so the preview tab
renders the user's real tabs while its own layout autosaves land in the copy and
never clobber the live layout. Keeping it on the per-app config rather than
reading the process env means two servers in one process (how the tests run
them) each resolve their own layout dir.

The README was updated to describe the live-editing flow (edit an isolated
worktree, build, refresh a labeled preview tab in place, then merge and reveal)
and the new `preview-refresh` sub-command for picking up a backend edit on the
preview's existing port.

A second system interface on the same host no longer breaks its own agent view.
`mngr observe` is single-writer per mngr host dir, so a preview booted against
the live workspace's host dir lost the lock, its observer exited seconds into
boot, and its agent list and chat panels stayed frozen at boot state forever --
every agent created afterwards rendered "No conversation data", while terminal
tabs (which proxy straight through) stayed current. The fix is a new
`system_interface_agent_events_mode` setting (`SYSTEM_INTERFACE_AGENT_EVENTS_MODE`):

- `OBSERVE`, the default, is unchanged -- run `mngr observe --stream-events` and
  consume its stdout.

- `FOLLOW` reads the event file the *running* observer writes, taking no lock.
  It begins folding at the newest full-state snapshot (replaying a lone
  per-agent update would collapse the agent set to that one agent), forwards
  only complete lines (a large snapshot exceeds the atomic append size, so the
  tail can hold a half-written line), and re-seeds if the file is truncated. If
  no process holds the observe lock there is nothing to follow, so it refuses to
  start rather than tailing a dormant file.

`AgentManager` now tracks whether lifecycle events are actually reaching it, in
either mode, and a new `GET /api/health` reports it: 200 only when a fresh mngr
discovery succeeds *and* the lifecycle stream is live, 503 otherwise. Merely
spawning the observe subprocess does not count as live -- an observer that loses
the lock exits without ever emitting -- so the stream is called alive only once
an event has arrived. `/api/agents` is unchanged; it runs its own discovery,
which is exactly why it answered 200 on an instance whose agent view was dead
and could not serve as the health gate.
