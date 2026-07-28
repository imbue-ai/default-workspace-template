Added the implementation plan for the system-interface live-editing flow at
`docs/system/blueprint/system-interface-live-editing/`: why the previous
delegate-then-preview-before-merge shape inverted "live first, ratify at
turn-end" for the one app that *is* the workspace UI, and how a lead-driven
worktree plus an in-place preview loop restores the fast loop without giving up
the never-serve-a-broken-build guarantee. The flow itself is described in the
`agents` and `system_interface` entries for this branch.
