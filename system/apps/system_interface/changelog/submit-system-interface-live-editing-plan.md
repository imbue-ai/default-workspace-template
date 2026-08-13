The README was updated to describe the live-editing flow (edit an isolated
worktree, build, refresh a labeled preview tab in place, then merge and reveal),
and to note that refreshing a live preview on its existing port and tearing it
down are the shared `serve_isolated_instance.py`'s own `refresh` / `down` --
addressed by the instance name `preview` prints (`si-preview-<slug>`) -- rather
than sub-commands of `reveal_system_interface.py`.

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

The follower itself now comes from mngr rather than living here. `mngr observe`
owns the event file's format, so the mechanics of tailing it (anchor the fold at a
full-state snapshot, forward only complete lines, re-seed on truncation, refuse to
start when no observer holds the lock) moved to `imbue.mngr.api.observe` as
`ObserveEventFollower` / `is_observe_writer_running` / `find_last_full_state_offset`.
`agent_events.py` keeps only what is specific to this app: which of the two sources
an instance uses (`AgentEventsMode`) and how it reports whether events are arriving
(`AgentEventsStatus`). Those invariants are the writer's, so they belong next to it
where a format change would be noticed.

`AgentManager` now tracks whether lifecycle events are actually reaching it, in
either mode, and a new `GET /api/health` reports it: 200 only when a fresh mngr
discovery succeeds *and* the lifecycle stream is live, 503 otherwise. In both
modes, "live" means an event has actually been folded -- merely spawning the
observe subprocess does not count (an observer that loses the lock exits without
ever emitting), and neither does starting a follower cleanly (it drops every
line until it has a full-state snapshot to fold from, so one attached to a
stream that has not emitted a snapshot sits frozen at boot-state discovery).
`/api/agents` is unchanged; it runs its own discovery, which is exactly why it
answered 200 on an instance whose agent view was dead and could not serve as the
health gate.

**Only the authoritative instance manages chat OOM scores.** A `FOLLOW`-mode
instance -- the preview, the reveal pre-flight -- is a read-only second view of a
workspace another instance owns, so it is now built without the capability to
write `oom_score_adj` at all, and it neither seeds nor runs the staleness sweep.
Otherwise two instances would fight over the same `/proc` entries, and the
preview would lose on the merits anyway: the frontend activity reports that
supply the open/visible bonuses go to the authoritative instance, so the
preview's writes would be both contending *and* worse. The capability is
withheld rather than the call sites gated because `reapply` is reachable in
`FOLLOW` mode by two paths that are easy to miss -- every folded lifecycle event
runs `record_running_agents`, and the preview serves its own frontend, which can
post `/api/activity` -- so a call site added later is inert by construction
instead of needing to remember a mode check.
