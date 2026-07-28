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
