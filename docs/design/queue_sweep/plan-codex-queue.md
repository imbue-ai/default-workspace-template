# Codex queue mirror: generation-scoped ledger replay + dead-process sweep

## Contract being enforced

Contract A (the mirror invariant) for the codex harness: the "queued messages" Minds
shows must exactly match the live codex process's in-memory parked steer queue. When the
process dies (mngr stop, SIGKILL restart, container loss) the mirror must show empty even
though the durable ledger still holds history; when only the system_interface backend
restarts under a live codex, the mirror must rebuild to the true parked set.

## Today's behavior and what is wrong

- The watcher replays `$CODEX_HOME/queued_input.jsonl` from byte 0 on every backend
  start (`_queued_offset = 0`, `harnesses/codex/watcher.py:199`; fold loop in
  `_consume_queued_input`, `watcher.py:343-380`) into `CodexQueueTracker`. Correct for
  backend-restart-under-a-live-codex *because* of the fork's conservation law: every
  `queued_input` gets exactly one `queued_committed`/`queued_retracted`
  (`queued_input_log.rs` in `patches/0.146.0.patch`).
- The conservation law fails when codex dies without writing its terminating records:
  the fork's restore-time retraction (`input_restore.rs`) only closes entries in the
  *current process's* memory; after a SIGKILL the next `codex resume` restores steers
  with `queued_id: None` (`user_messages.rs`), orphaning the on-disk records forever.
  Minds' own interrupt IS a SIGKILL (`_restart_agent_process`, `server.py:711-728`, via
  `_drain_queue`, `server.py:749-772`), so `queue_tracker.py:18-21`'s "self-cleans on
  resume" premise is false for every kill-based death.
- No liveness gate, no process-session scoping -> orphans resurrect. Two violations:
  1. A stopped codex agent killed mid-turn (e.g. container restart): derive ignores
     `is_agent_running` (`harnesses/codex/activity.py:50-53`) and the tail is not stale
     (`codex_process_started` never re-touched, `activity_state.py:199-223`), so
     activity reads THINKING -- and even at IDLE, no recompute runs after the replayed
     snapshot reaches the manager (move 3) -- so the level-triggered idle sweep
     (`agent_manager.py:1369-1403`) never fires; the phantom queue persists forever.
  2. A running agent after a backend restart: prior-generation orphans fold in alongside
     the genuinely-parked current entries; the agent is not IDLE so no sweep fires, and
     the flush/drain block (`queued_set.py:112-119`) would include ghost text.
- Already right, NOT to be torn out: the full-ledger replay itself (what makes backend
  restarts rebuild truth), by-id resolve (`queued_set.py:90-98`), `clear_queue` after
  our own SIGKILL restart (`server.py:771`), and the IDLE sweep itself.

## Minimal change

Three moves, all backend-side. No new files, no new protocol, no fork or mngr change --
the discriminators already exist on disk.

1. **Generation-scope the replay** (`watcher.py:_consume_queued_input`). Once per batch,
   stat the existing `codex_process_started` marker (touched by mngr on every codex
   launch, `mngr_codex/plugin.py:1052-1057`, filename `codex_config.py:162`) and skip
   folding any ENQUEUE whose ledger timestamp (RFC3339 UTC, `rfc3339_now` in
   `queued_input_log.rs`; parse via `parse_iso_timestamp_to_epoch`,
   `activity_state.py:150`) predates the marker mtime; LEAVE records still fold
   unconditionally (unknown-id resolve is a no-op). Nothing live is ever dropped (a live
   entry postdates its own launch); no orphan survives a *relaunch*. Orphans of an agent
   that died and was never relaunched postdate the last marker touch -- moves 2-3 sweep
   those. Missing marker or unparseable timestamp: fold (positive-evidence rule, as in
   `is_transcript_tail_stale`). Tear out: the false "self-cleans on resume" premise in
   `queue_tracker.py:18-21`.
2. **Dead lifecycle forces IDLE, at the manager** (`agent_manager.py:1364-1367`). Claude
   and pi already gate derive on `is_agent_running`
   (`harnesses/claude/activity_state.py:43`; `harnesses/pi_coding/activity.py:38-45`),
   so the existing sweep condition (`agent_manager.py:1378-1379`) covers them; codex
   alone ignores the lifecycle (`harnesses/codex/activity.py:50-53` -- deliberate for
   the RUNNING/WAITING turn flap, not the dead/alive axis the agent list already
   trusts). Rather than a second, codex-only liveness gate in the sweep condition,
   override in `_recompute_activity_state`: when `agent_state.state` is dead -- outside
   `RUNNING_LIFECYCLE_STATES` (`activity_state.py:166`), `WAITING`
   (`activity_state.py:178`), and `UNKNOWN` (provider unreachable is not death; never
   wipe on it, `mngr/primitives.py:282-297`) -- force `new_state` to IDLE. Sweep and
   derive signatures untouched (the sweep still uses `app_context.py:123` ->
   `watcher.notify_idle`); the same line fixes the phantom "Thinking" dot on a dead
   codex agent.
3. **Give the sweep a trigger** (`agent_manager.py:1210-1230`). The sweep is evaluated
   only inside `_recompute_activity_state`, and no recompute runs after a replayed
   snapshot reaches the manager: the `on_events` fan-out (`watcher.py:259`, the only
   path into the tracker's one latched recompute) runs strictly before the snapshot push
   (`watcher.py:263`), and a permanently-dead agent never re-enters `state_changed_ids`
   (`agent_manager.py:1004-1010`) -- the sweep is unreachable exactly when needed. Fix:
   `update_queued_messages`, after caching a changed snapshot and before its existing
   broadcast, calls `_recompute_activity_state(agent_id, broadcast_on_change=False)`
   (outside the manager lock); its single broadcast then carries the post-sweep state,
   so a dead generation's orphans are swept in the cycle they arrive and never rendered
   -- no ghost-bubble flash on backend restart.

Rejected: a fork-side startup pass retracting orphans (duplicates policy, forces
rebuild+repin, backend still needs the dead gate); a replay-offset-at-launch file (new
machinery when marker mtime + ledger timestamps already discriminate); `thread_id`
scoping (threads persist across restarts -- not a generation boundary); a liveness gate
inside the watcher (no lifecycle input there; move 3's pre-broadcast sweep already keeps
phantoms off the wire).

## Ship mechanics

All edits in `system/apps/system_interface` -- lands on `claude-codex-pi-dwt`. No change
to the vendored mngr tree and no codex-in-minds patch change, so no rebuild or sha256
repin in `system/scripts/setup_system.sh`.

## Tests

- `harnesses/codex/watcher_test.py`: ledger mixing a prior-generation open entry with
  current-generation open/committed/retracted records, marker mtime between generations
  -> snapshot is exactly the current open set; marker absent -> everything folds; a LEAVE
  for a filtered id is a no-op; unparseable enqueue timestamp folds.
- `agent_manager_test.py`: integration-shaped -- a snapshot arriving via
  `update_queued_messages` AFTER the last recompute, for a STOPPED agent with a non-IDLE
  transcript-derived state, is swept before broadcast (no broadcast ever contains the
  phantoms) and activity broadcasts IDLE; UNKNOWN neither sweeps nor forces IDLE; a
  RUNNING mid-turn agent's snapshot passes through unchanged. The arrival-order shape
  matters: seeding the cache by hand would pass without the move-3 trigger.
- Final check per repo convention: `cd system/apps/system_interface && uv run pytest`
  (full suite, coverage on).

## Open risks

- Marker mtime vs ledger timestamps assumes one monotonic host clock; a backwards step
  across a restart could mis-scope. Accepted -- the identical comparison already backs
  `is_transcript_tail_stale`.
- The ledger grows without bound and is re-read from 0 each backend start; the guard
  adds no cost but does not shrink it. Fork-side truncation is a separate task.
- A lifecycle misreport of STOPPED while codex lives falsely sweeps, and the damage does
  NOT self-heal: `notify_idle` clears the tracked set (`watcher.py:548-560`,
  `queued_set.py:100-102`) but the sidecar cursor has passed the swept enqueues, so they
  never re-enter the mirror this backend lifetime -- a later stop either no-ops on the
  empty block (`server.py:762-763`) with codex still holding the steers, or (after a
  fresh enqueue) hands back only the new entry while the SIGKILL destroys the older
  ones. Accepted with the blast radius stated: mngr maps probe/provider failures to
  UNKNOWN (excluded from the dead set), so a false STOPPED requires mngr to positively
  misreport a death -- the signal the agent list already renders as ground truth.

## Non-goals

- Contracts B and C for codex (stop button and atomic shoulder tap already ship; any
  gaps are sibling plans in this series).
- The pi and claude mirrors (sibling plans). Move 2 is shared infrastructure owned here;
  move 3 is the same single edit plan-claude-queue owns and lands first (specified in
  both plans for their own rationales, landed once).
