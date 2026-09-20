The chat app follows phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- The chat's instances API (`/_instances` and the per-instance rename, delete, location, stop, and start routes), its shell nudge, and its `subagent` action are deleted; the chat page and the chat root are known to the shell only as windows at their paths. `/api/health` is what the update apply probes after a restart.

- The client-activity report the chat forwards to the shell carries `desktop_id` (the desktop the reporting page's window is on, from the shell handshake) in place of the view id.

- The manifest declares launch paths only; the retired `instances`, `instances_url`, and `actions` keys are gone.

- The frontend no longer imports the contract module's `open(address)`, which is deleted; every open goes through `openPath`. The retired-address ratchet now also refuses `app:` literals in the chat frontend, and the comments that still described the page as a dockview tab describe a window.
