# pi queue mirror: scope to the live process generation

## Contract being enforced
Contract A (the mirror invariant): what Minds shows as "queued messages" for a pi agent must
exactly equal the live pi process's actual parked steer queue, scoped to the current process
session. Process dead (stopped, crashed, container restart) means the queue is gone and the
mirror shows empty -- durable files may keep history, but the backend must never show it. A
system_interface restart while pi keeps running must still rebuild the true queue.

## Today's behavior and what is wrong (precise file:line citations)
The mirror is rebuilt by replaying two durable files with no process-session scoping:

- Enqueues: `harnesses/pi_coding/watcher.py:139` seeds `_inbox_offset = 0` at build, so
  `_consume_inbox` (watcher.py:269-309) replays every string line of `pi_inbox` ever written
  and enqueues each (watcher.py:308). The pi extension, by contrast, seeds
  `processedInbox = countLines(inboxPath)` at load
  (`system/vendor/mngr/libs/mngr_pi_coding/imbue/mngr_pi_coding/resources/mngr_pi_lifecycle.ts:425-426`),
  so lines written before the current process launch are never injected. The mirror and the
  live process therefore disagree by construction.
- Leaves: `_ingest_event` pops one FIFO head for every `user_message` ingested
  (watcher.py:325-326), including drains performed by long-dead process generations.
- The residue survives the replay as phantom "queued" entries.

The sweep that should clear them exists in full -- the level-triggered idle backstop in
`agent_manager._recompute_activity_state` (agent_manager.py:1369-1403), which derives IDLE for
any non-running agent (claude/activity_state.py:43-44, reused via pi_coding/activity.py:38-45)
and invokes the registered `notify_idle` handler. It just never fires at the right time: the
only post-restart recompute runs at watcher creation (app_context.py:131), before the
2s-debounced snapshot (watcher.py:65, 187-201) lands, and `update_queued_messages`
(agent_manager.py:1210-1230) caches and broadcasts with no recompute. Result: a stopped pi
agent shows a permanent phantom queue -- the bug the user hit live.

## Minimal change (tear out X, replace with Y)
No new files, no new protocol, no agent_manager code in this plan.

**1. mngr pi extension: generation-scope the durable inbox at load** (mngr_pi_lifecycle.ts).
Immediately before seeding `processedInbox` (ts:425-426): append `pi_inbox`'s current content
to a sibling `pi_inbox_history` (raw history preserved), then truncate `pi_inbox` in place.
The seed then reads 0 from the empty file (the `countLines` call stays). The durable inbox now
contains only current-generation lines BY CONSTRUCTION, so the watcher's replay-from-zero is
already generation-scoped -- no boundary file, no floor arithmetic, no counting-base change.
Safe against races: mngr appends to the inbox only after the readiness sentinel
(plugin.py:671-695; ts:408-413), which `session_start` writes after load -- the same property
the existing `countLines` seeding already relies on. (A recorded-offset "floor file" was the
previous design: rejected as a new cross-repo artifact requiring a physical-line re-basing
reconciling three counting schemes; truncation deletes all of that and keeps `_queued_id`'s
basis, queue_tracker.py:41-49.)

**2. Watcher: scope leaves to the current generation** (dwt side).
- Tear out: the unconditional pop in `_ingest_event` (watcher.py:325-326). Replace: pop only
  when the `user_message` timestamp (session_parser.py:134-137) parses to >= the
  `pi_process_started` marker mtime (plugin.py:162, touched by the launch prelude at
  plugin.py:651-661) -- the exact boundary and comparison the activity path already uses
  (`is_transcript_tail_stale`, activity_state.py:200-223; plan-claude-queue does the same for
  claude). Stat the marker once per refresh. Missing marker or unparseable timestamp -> pop
  (today's behavior; over-popping errs toward an empty mirror, the contract-safe direction).
- Enqueue side: unchanged. The generation reset is the EXISTING shrink-reset
  (watcher.py:280-284): the truncation shrinks the file; the reset runs at the top of the
  locked consume cycle, rewinds the byte cursor, and rebuilds ids from index 0 -- ordered
  correctly by construction, no new reset path.

**3. Manager: the missing sweep trigger is a sibling plan's edit.** plan-claude-queue adds one
`_recompute_activity_state` call at the end of `update_queued_messages` and explicitly covers
pi ("closes the identical trigger hole for the codex and pi mirrors"). That single shared edit
owns the harness-agnostic sweep; this plan adds no gate, no duplicate liveness predicate, and
no agent_manager tests. The debounce keeps its documented job (suppressing idle-agent-send
flicker before anything is broadcast) with no second gate to overlap.

Everything else stays: `notify_idle`, `clear_queue`, `_drain_queue` (server.py:749-772), the
pi shoulder-tap sentinel branch (server.py:850-864), QueuedSet.

## Ship mechanics (which repo/branch/rebuild)
- Extension change in the vendored tree `system/vendor/mngr/libs/mngr_pi_coding/` --
  **lands on the claude-codex-pi-mngr branch**.
- Watcher change in `system/apps/system_interface/` -- lands on claude-codex-pi-dwt, and
  depends on plan-claude-queue's `update_queued_messages` trigger landing there too.
- Existing pi agents were provisioned at create only (plugin.py:858-883): a one-off migration
  rewrites each agent's `<agent_state_dir>/commands/mngr_pi_lifecycle.ts` from the new
  resource (takes effect at the next launch).
- No codex fork change; no rebuild or sha256 repin.

## Tests
- `system/apps/system_interface/imbue/system_interface/harnesses/pi_coding/watcher_test.py`:
  a `user_message` older than the marker mtime does not pop; a current-generation one does;
  missing marker pops (today's behavior); truncation-then-append replays exactly the appended
  lines with ids re-based from 0.
- `system/vendor/mngr/libs/mngr_pi_coding/imbue/mngr_pi_coding/resources/mngr_pi_lifecycle_test.py`:
  at load, prior inbox content lands in `pi_inbox_history` and `pi_inbox` is empty; pre-existing
  lines are still never injected (extend `test_inbox_watcher_injects_only_new_lines`).
- agent_manager sweep tests are owned by plan-claude-queue / plan-codex-queue.

## Open risks
- Un-migrated agents (old extension) degrade to the full-history replay: the stopped-agent
  bug is still fixed by the shared sweep, but a backend restart while such an agent is
  mid-turn shows the historical residue for the rest of the turn, and a stop click in that
  window would return historical text to the composer (`_drain_queue` reads the watcher
  tracker directly, server.py:761). The one-off migration closes this.
- Stale-lifecycle false sweep (pre-existing, shared with the siblings): the sweep derives
  IDLE from the observe-reported state, which can read STOPPED during the lag after an
  auto-start (agent_discovery.py:223); a message parked in that window is destructively
  swept -- hidden until it drains, with the stop button a silent no-op meanwhile
  (server.py:761-763 returns early on an empty block). Corroborating on the `active` marker
  was REJECTED: a mid-turn death leaves the marker stale-present (the reported live
  scenario), so corroboration would unfix the primary bug; non-destructive suppression would
  leave a dead agent's tracker feeding `_drain_queue`/`_flush_queue`. The window is one
  observe refresh, usually shorter than the 2s debounce.
- Intra-generation enqueue/leave collapses break the 1:1 pairing for a backend restart
  mid-turn: the atomic shoulder tap merges N parked lines into one resubmitted `user_message`
  (ts:454-481), and after a `/new` the replay reads only the current session file
  (watcher.py:227-236), so pre-rotation drains never pop. Phantoms then show for the rest of
  the turn, and a stop click would hand back already-delivered text. Accepted residual
  (narrow window; the idle sweep clears it at turn end); a mid-generation re-truncation was
  deferred (it could drop lines appended while the drain is paused). The live-tap variant of
  the same mispairing belongs to plan-pi-interrupt.
- A post-truncation append burst exceeding the prior generation's byte length within one
  watcher poll would mask the shrink; consequence is a warning-skipped garbled line. Judged
  negligible (tracking `st_ino` is the fallback if ever observed).
- Leave scoping compares session timestamps to a marker mtime -- same-host clocks (the
  established `is_transcript_tail_stale` assumption); a large clock step could misclassify
  a drain, which the idle backstop later sweeps.

## Non-goals
Contracts B and C (covered by sibling plans); the codex and claude mirrors (same structural
problem, separate plans); the shared `update_queued_messages` trigger (owned by
plan-claude-queue); changing steer/followUp delivery, the debounce window, or QueuedSet
semantics.
