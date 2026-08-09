# Claude queuing: scope the queue mirror to the live process session

## Contract being enforced

Contract A (the mirror invariant): the queued messages Minds shows for a claude agent must
exactly match the live claude process's in-memory parked queue, which dies with the process.
Two restart cases must be distinguished: a backend restart with claude alive mid-turn (mirror
MUST be rebuildable from the durable ledger) versus a claude process stop/restart (mirror
must show empty even though the ledger still holds history).

## Today's behavior and what is wrong

The mirror is derived by replaying claude's in-transcript `queue-operation` ledger from byte 0
of every main session file (`harnesses/claude/watcher.py:756-771` feeds `parse_queue_signals`
into `ClaudeQueueTracker.consume`). The tracker's only process-generation discriminator is a
reset when a signal arrives bearing a NEW session id (`harnesses/claude/queue_tracker.py:80-82`).

Three holes:

1. Stopped agent shows a ghost queue. After a container restart with the agent left stopped,
   the priming replay repopulates the tracker with the dead process's dangling enqueues (no
   matching leaves) and broadcasts them (`watcher.py:866-874`, `agent_manager.py:1210-1230` --
   no liveness gate). The idle sweep (`agent_manager.py:1369-1403`) is level-triggered, but a
   queue-snapshot arrival triggers no `_recompute_activity_state` call, and a stopped agent
   produces no further triggers -- the ghost queue sticks indefinitely.

2. Replay feeds every dead session's ledger. The session-id reset fires only when the
   post-restart session emits a queue signal. A dead session's dangling enqueues sit in the
   ledger forever; a later full replay (backend restart, or the truncation reset at
   `watcher.py:722-733`) re-derives them, and if claude is alive mid-turn the idle sweep
   never fires. And after a restart outside the minds endpoints (mngr CLI, external kill),
   residue already in the set stays through the whole first post-restart turn: the new
   session may emit no queue signal (no reset) and the agent is RUNNING (no sweep).

3. Activity seeding races the watcher thread. `watcher.start()` runs before the
   transcript-signal seeding (`app_context.py:125` vs `:131`), and an unseeded tracker
   derives IDLE even for a running mid-turn agent (`harnesses/activity.py:72-74` defaults;
   `harnesses/claude/activity_state.py:43-51`, rule 4). Today a narrow race; move 1 would arm
   it on every priming broadcast, so it must be fixed first -- otherwise the sweep destroys
   the genuine mirror in exactly the backend-restart-mid-turn case, and the stop button then
   no-ops (`server.py:761-763` returns early on an empty block).

What must be preserved: the replay itself -- a backend restart under a live mid-turn claude
has no other way to rebuild the true queue (enqueues net against leaves, exactly as today).

## Minimal change

Two small moves; no new files, no new protocol, no mngr change, no `session_parser.py`
change, no marker read, no timestamp comparisons.

1. Add the missing sweep trigger -- after fixing the seeding order.
   - Reorder `app_context.get_or_create_watcher`: call `update_session_events` (seeding needs
     no running watcher thread -- `get_all_events` reads synchronously) BEFORE
     `watcher.start()`, keeping the seeding call outside the watchers lock as today. No
     recompute can then ever see an unseeded tracker alongside a cached queue snapshot.
   - Then, in `AgentManager.update_queued_messages` (`agent_manager.py:1210-1230`), after
     caching a changed snapshot and BEFORE its broadcast, call `_recompute_activity_state`
     (exact form per plan-codex-queue move 3: `broadcast_on_change=False`, one broadcast
     carrying the post-sweep state, so phantoms are never rendered). The existing backstop
     (`:1369-1403`) does the work: a stopped/idle agent derives IDLE and the snapshot is swept
     via `notify_idle` (`watcher.py:1184-1196`); a live mid-turn agent derives non-IDLE
     (seeded signals) and the snapshot stands. This also closes the identical trigger hole
     for the codex and pi mirrors (their plans assume it).

2. Scope the replay to the current process generation using the ledger's own session id. The
   discriminator already exists durably: mngr's SessionStart hook appends every new session id
   to `claude_session_id_history` (`system/vendor/mngr/libs/mngr_claude/imbue/mngr_claude/claude_config.py:749`),
   a claude restart always rotates into a new session file (`queue_tracker.py:64-67`,
   `watcher.py:523-524`), and the watcher derives the chronological main-session list every
   cycle (`watcher.py:981-1021`) -- so the live process's queue is exactly the latest main
   session's queue signals. Changes, confined to `watcher.py` and `queue_tracker.py`:
   - Narrow the queue-feed gate at `watcher.py:758` from membership in `_main_session_ids` to
     equality with its LAST entry. The gate lives at the single feed point
     (`_ensure_cache_current`), so every replay path -- priming (`:896-913`), truncation reset
     (`:722-733`), HTTP-read feeds (`:499-516`), and the poll loop -- inherits it. Event
     routing (`is_main_session_event`, `:484-497`) keeps membership; only the queue feed
     narrows. Use the same latest-only predicate for the truncation reset at `:732`.
   - Reset the queue tracker when a NEW latest main session is registered
     (`watcher.py:1017-1021`): a new latest session means the process restarted, so residue
     already consumed from the dead session is purged within one discovery cycle, without
     waiting for the new session to emit a queue signal (closes hole 2's residue window).
   - Delete the tracker's internal session-id reset and `_session_id` field
     (`queue_tracker.py:80-82`): `consume` now only ever sees latest-session signals, and a
     change of latest resets at registration before that session can feed.

Case check: (a) stopped agent -- the latest session's dangling enqueues still load; move 1's
trigger sweeps them immediately (derive is IDLE). (b) claude restart then later replay -- dead
sessions never feed, and registering the new latest purges any live residue. (c) backend
restart, claude alive mid-turn -- the parked enqueues are in the latest session (a queue
cannot span sessions) and replay exactly; seeded signals derive non-IDLE, no sweep.

## Ship mechanics

All changes are in `system/apps/system_interface` (dwt side) and land on `claude-codex-pi-dwt`.
No change lands on `claude-codex-pi-mngr`: the session-id history file already exists in the
vendored mngr. No codex fork change, no rebuild, no sha256 repin.

## Tests

- `harnesses/claude/queue_tracker_test.py`: update for the removed session-id reset; a
  single-session replay reproduces the true queue; `reset()` clears.
- `harnesses/claude/watcher_test.py`: a fresh replay over a dead session's dangling enqueues
  plus a newer main session snapshots empty; the latest session's parked enqueues replay
  (backend-restart case); a new main session registered mid-watch purges residue on the next
  cycle; a truncated latest session re-derives from scratch.
- `server_test.py`: `get_or_create_watcher` seeds activity signals before starting the watcher.
- `agent_manager_test.py`: a non-empty queued snapshot arriving while the derived state is
  IDLE triggers the sweep and broadcasts the drained snapshot; the same snapshot with
  seeded mid-turn signals (derive non-IDLE) is kept.

## Open risks

- Inherits the codebase's existing assumption that a session rotation implies a process
  restart (`queue_tracker.py:64-67`). A mid-turn rotation without a restart (e.g. compaction
  rotating the id) would clear the mirror early -- same family as today's session-id reset.
- The history append rides a SessionStart hook behind `MAIN_SESSION_ONLY_GUARD`; if
  suppressed, the new session is never discovered at all, hiding transcript and queue alike
  (pre-existing, larger than the queue).
- One brief window: after a fast external restart, residue can show until the new session's
  file appears and is registered (about one poll cycle). (The pre-broadcast sweep ordering
  removes the stopped-agent ghost-queue flash.)
- `update_queued_messages` gates on `_activity_tracked_agents`; a priming broadcast that beats
  observe-driven tracking startup is dropped and not re-pushed (pre-existing, unlikely:
  watchers are created by HTTP requests that follow the agent-list broadcast).
- (Rejected: timestamp-filtering against the `claude_process_started` mtime -- heavier, and
  the session-id gate encodes the same boundary durably.)

## Non-goals

- Contracts B and C (stop-button retraction, shoulder-tap commit) -- separate plans.
- The codex and pi populators/watchers -- sibling plans (they inherit move 1).
- Frontend changes, durable-ledger format changes, and any mngr or codex-fork change.
