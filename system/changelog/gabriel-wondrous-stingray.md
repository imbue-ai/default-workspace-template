Fixed a family of app-name bugs found while updating a workspace to minds-v0.6.2.

The reserved-name set is carried in four places, because three of them cannot import the one that owns it: `forward_port.py` is stdlib-only by contract, and `layout.py` and `migrate_workspace_layouts.py` are agent-facing scripts. `layout.py`'s copy had drifted to two of the eight names, so a bare word like `share` or `host` waited five seconds for a registration that could never happen instead of being refused at once, and the layout migration carried the same two-name copy. The drift test that was supposed to catch this sampled a fixed list of names rather than comparing the sets, so it could not see the gap; it now compares every carrier's set.

`github` is now reserved. Enabling GitHub sync writes a `github-sync` supervisord program, and an app named `github` would have claimed it as a sidecar -- so turning GitHub sync on would have failed the manifest suite in that workspace.

Apps can no longer collide with *another* app's programs: an app named `pr` would have claimed a second app's `pr-review` program as its own sidecar. The existing guard only covered collisions with programs that belong to no app. An app matching itself is not a collision -- a manifest may set `program` to its own `<name>-<role>` form, and that program is its sidecar.

`layout.py`'s tests now run without any part of the ambient agent identity. They already ran with the chat id hidden, but `layout.py` falls back from it to `MNGR_AGENT_ID`, and nothing hid that one -- so a test asserting on an address `layout.py` derives could still be reading whoever ran the suite rather than its own inputs. Both are cleared now, in one fixture rather than two, matching what the browser app's tests needed.

The agy shim's open-steps reminder no longer depends on GNU `stat`, and its tests no longer depend on the kernel's inode allocator. The turn key is the `active` marker's inode, read with `stat -c`, which only GNU stat understands -- so off Linux the probe returned nothing, the reminder never fired, and the suite looked broken rather than unportable. Both spellings are now asked.

The test that checks the reminder returns on a new turn used to recreate the marker and hope for a different inode, holding the freed number down with 64 files. When the kernel reused it anyway the reminder correctly stayed quiet and the test failed -- a red that was reported as a shim bug. It now renames a confirmed-new inode into place, and says so plainly if it cannot get one.

The fixture those tests share also never started the step it created, so the tickets dir held a declared-but-unstarted step rather than an open one, and the shim emitted the "none is currently in_progress" reminder instead. It now starts the step, and checks both tk calls rather than discarding their output.

AGENTS.md now states that finding a defect in built-in code is itself a reason to escalate it upstream, names the two channels (a report when you have a diagnosis, a PR when you have a fix), and says plainly that a local ticket is not a valid end state for one.
