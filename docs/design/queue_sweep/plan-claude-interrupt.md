# claude stop button: chord interrupt on an empty queue, restart-drain on a nonempty one

Status: SHIPPED -- landed on `claude-codex-pi-dwt` (`9f2dd027`, merged `c9a5b571`).

## Contract being enforced

Contract B: stop interrupts the running turn, returns queued messages to the user's composer,
and leaves the harness's own queue empty. For claude the contract splits cleanly on the queue
mirror. NONEMPTY: the restart-drain base already satisfies every clause -- and must stay: under
the tap plan's working assumption `chat:cancel` FLUSHES the parked queue through, the chord on
a nonempty queue would commit the very messages stop promises back. EMPTY: nothing to retract,
so the contract reduces to a pure interrupt, which the tap plan's Chat-only `meta+q` chord does
natively -- no SIGKILL mid-tool-write, no relaunch/resume cost, no flash. One button, one
endpoint, no wire-visible flag: the branch is backend-internal.

Sequencing: after plan-claude-queue (the branch trusts the mirror), plan-pi-interrupt (the
interrupt-to-composer registry), and plan-claude-tap (chord provisioning, dwt executor module,
`is_tap_binding_active()` predicate -- all reused here).

## Today's behavior and what is wrong (precise file:line citations)

- `/drain-to-composer` (`server.py:888-909`) runs `_drain_queue` (`server.py:749-772`) for
  EVERY harness: capture the block, `mngr start --restart --no-resume` (`server.py:711-728`),
  `reset_activity_state` + `clear_queue`. Empty block returns early (`server.py:761-763`), so
  an empty-queue stop silently no-ops.
- **Amendment to plan-pi-interrupt:** that plan hoists the empty-block short-circuit so the
  BASE restarts even with nothing queued. The hoist stays load-bearing here -- the chord
  replaces the restart only on the happy empty-queue path; claude's delegations to the base
  (dialog gate, binding-inactive, deadline fallback, all below) still restart with an empty
  queue, so do NOT re-add an empty-block early return for claude. The pi plan's pinned test
  "claude drain-to-composer with an EMPTY queue now restarts" is REPLACED by the dispatch
  tests below; the two plans cannot both land as written without this substitution.
- After a native interrupt the indicator lies for ~60s: claude fires NO hook on a user
  interrupt (the Stop hook is documented not to run on interrupt), stranding the `active`
  marker UserPromptSubmit created (`claude_config.py:803-804`). The recompute stats it every
  pass (`agent_manager.py:1357`) and `resolve_is_agent_running` keeps answering True
  (`activity_state.py:182-196`; for claude, RUNNING lifecycle iff marker,
  `claude_config.py:694`) until the Notification idle_prompt hook clears it
  (`claude_config.py:853-863`).
- The interrupt sentinel has TWO shapes on disk (verified in real session files on this host):
  `[Request interrupted by user]` (streaming abort) and `[Request interrupted by user for tool
  use]` (abort while a tool runs -- the DOMINANT stop scenario; the plain constant is NOT a
  substring of it). The parser suppresses only the first, by exact match
  (`session_parser.py:61,:448`); the mid-tool variant renders as a phantom user bubble and,
  per the parser's own rationale (`session_parser.py:56-60`), pins the tail heuristic. This
  plan extends the suppression. Rule 0 of claude's derive (`harnesses/claude/activity_state.py:43-44`)
  drops the indicator the moment `is_agent_running` goes False -- the marker-clear settles it.

## Minimal change (tear out X, replace with Y)

Tear out: claude's fall-through to the base restart-drain. Replace: claude registers a native
override in plan-pi-interrupt's registry; the override lives in the tap executor module
(plan-claude-tap move 4) and reuses its machinery wholesale -- refresh-first, live-session
resolution, byte-size baseline, one `tmux send-keys M-q` under mngr's `message.lock`
(`base_agent.py:368-394`), the tap's shared ~200ms watch loop with the deadline as its only
parameter. Only the verdict path differs:

1. Refresh: `get_all_events()`; read the mirror (`watcher.py:1170-1172`).
2. Mirror NONEMPTY -> delegate to the registry's base restart-drain unchanged (it captures,
   restarts, resets, clears, returns the block). Delegate likewise when `permissions_waiting`
   is present (`claude_config.py:823-852`: the Chat chord is inert under a dialog; the tap
   409s there, but stop must work -- a blocked turn is still a turn) or when
   `is_tap_binding_active()` is False.
3. Mirror EMPTY and `active` marker absent -> `{block: ""}` 200: no turn, nothing queued,
   composer untouched (the frontend writes only on a non-empty block -- the `if (block)`
   guard, `MessageInput.ts:251`; the inner composer-empty lines are rewritten to merge by
   plan-pi-interrupt move 4).
4. Acquire `message.lock`, then RE-RUN steps 1-3 under it before `send-keys`: an in-flight
   mngr send holds that lock through its whole paste-and-confirm cycle, and a mid-turn send is
   confirmed exactly when its enqueue record lands (`tui_agent.py:135`, `plugin.py:2404-2417`)
   -- so a send-then-regret stop would otherwise read EMPTY, block on the lock until the
   message is durably parked, then chord-flush the very message stop promised back. Mirror now
   nonempty -> release the lock and delegate to the base. Then deliver the chord and watch
   (tap loop, 8s deadline), reading BOTH signals each poll:
   - Post-baseline abort evidence -> CONFIRMED. The predicate is pinned to the parsed record
     shape, never a raw substring: a `type=="user"` record whose text content block equals a
     sentinel variant (prefix `[Request interrupted by user`, both shapes above). A
     `tool_result` quoting the sentinel text must not confirm: agents here routinely grep
     session JSONL, and a false confirm strands the indicator IDLE mid-turn -- the exact
     inversion confirm-before-clear exists to prevent. Only now clear the markers and append
     the observe-poke event -- the shared idle-marking cleanup
     (`_CLEAR_ACTIVE_MARKERS_AND_EMIT_ACTIVITY_EVENT`, `claude_config.py:650-652`; same ops as
     `wait_for_stop_hook.sh` `mark_inactive`, sh:140-147). Return `{block: ""}`. The tap
     plan's decision-gate trace must also record a mid-tool interrupt to pin both shapes.
   - Marker vanished, no sentinel -> the turn ended naturally in the gap and its own Stop-hook
     path cleared it; return `{block: ""}`, clear nothing, restart nothing. 8s covers
     `wait_for_stop_hook.sh`'s floor when no other Stop hooks run (GRACE_PERIOD=3 + transcript
     flush before `rm -f active`, sh:137-141,236); with other Stop hooks provisioned (this
     workspace has two) the hook-wait loop (sh:250-276, MAX_WAIT=120) can exceed ANY deadline,
     so this arm is best-effort, not a guarantee.
   - Deadline, marker still present -> the chord may have been eaten (an ungated dialog
     state); fall back to the base restart-drain and return its result. Unlike the codex
     plan's rejected fallback this wires nothing new: the base is already in hand for branch 2.
   Confirm-before-clear holds on every path: markers are cleared only on abort evidence; the
   restart paths settle via their own `reset_activity_state`.
5. The marker-clear is an mngr_claude primitive, not dwt-direct file ops. Clearing the
   `active` marker and emitting the observe-poke activity event is claude-lifecycle
   machinery -- the exact ops the claude hooks already own
   (`_CLEAR_ACTIVE_MARKERS_AND_EMIT_ACTIVITY_EVENT`, `claude_config.py:650-652`, shared by the
   Stop / idle_prompt / SessionStart hooks). Per the same boundary the tap's keypress follows
   (harness-native mechanics live in mngr; dwt orchestrates), add a "mark this claude agent
   idle" primitive in mngr_claude that unlinks `<agent_state_dir>/active` + `permissions_waiting`
   and appends one activity line to the host activity log -- reusing the hooks' shared snippet as
   the single source of truth for the format -- and have the dwt executor CALL it (through the
   same in-process mngr boundary the keypress uses) once the abort is confirmed. This makes the
   interrupt executor the fourth caller of the hooks' own idle-marking, in the repo that owns it,
   rather than re-expressing the marker semantics in dwt. The event is load-bearing: it pokes
   `mngr observe` to re-probe; lifecycle flips RUNNING->WAITING; that observe change triggers
   `_recompute_activity_state` (`agent_manager.py:1061-1071`), which stats the now-absent marker
   (`:1357`) -> `is_agent_running` False -> derive rule 0 -> IDLE. No dwt derivation change.
6. Extend the sentinel suppression at `session_parser.py:448` to both variants
   (prefix-anchored exact records, matching its existing posture) -- without it every mid-tool
   stop leaves a phantom user bubble in chat.
7. **Amendment to plan-claude-tap:** the stop override records an in-process per-agent stop
   timestamp (module-local; both executors share the module) and the tap's NEEDS_RECOVERY arm
   suppresses its recovery send when a stop ran since the tap's baseline. Without this, a stop
   pressed inside the tap's 3s watch matches the recovery signature exactly (mirror drained +
   post-baseline sentinel, plan-claude-tap.md:96-98) and the recovery message re-drives the
   just-stopped messages -- actively un-doing the stop. One guard, no wire change.
8. Frontend: no change. `DrainToComposerResponse{block}` is unchanged; errors alert
   (`MessageInput.ts:260-266`); `isInterruptInFlight` hides the button while in flight
   (`MessageInput.ts:239-244`).

## Ship mechanics (which repo/branch/rebuild)

dwt only (registry entry, executor verdict path, parser suppression, tap recovery guard,
tests) -- lands on **claude-codex-pi-dwt**, after plan-claude-queue, plan-pi-interrupt, and
plan-claude-tap (whose mngr provisioning on claude-codex-pi-mngr this reuses). Nothing new on
mngr; no codex fork change, no rebuild.

## Tests

- Executor: nonempty mirror delegates to the base (no chord); dialog-marker and
  binding-inactive delegation; empty + no `active` marker no-ops; the under-lock re-check
  delegates when the mirror filled while waiting; confirmation on EACH sentinel variant clears
  both markers and appends a format-conformant event line; a `tool_result` containing the
  sentinel text does NOT confirm; marker-vanish arm clears and restarts nothing; deadline arm
  calls the base. (Chord delivery and its lock are the tap module's own tests; the live chord
  is verified manually via tmux -- repo convention: not crystallized.)
- Tap module: recovery suppressed when a stop ran since the baseline.
- `session_parser_test`: the mid-tool variant is suppressed; ordinary user text still passes.
- `server_test.py`: claude requests dispatch to the override; replaces the pi plan's pinned
  claude-empty-queue-restarts test; pi and codex arms unchanged (existing tests pass).

## Open risks

- **Pivot** (mirrors plan-claude-tap): if the tap's gate shows `chat:cancel` RETURNS the queue
  to claude's composer, the chord serves the nonempty branch too -- capture the block, chord,
  confirm, clear markers + `clear_queue`, return the block -- and the restart-drain loses its
  last claude caller. One decision, no dual design.
- The deadline fallback can SIGKILL a claude whose turn ended naturally but whose Stop-hook
  chain is still running -- with other Stop hooks provisioned that race lands here routinely,
  killing the chain's in-flight work (transcript flush, post-completion uploads, notify).
  Accepted: nothing queued is lost, no cheap signal distinguishes chord-eaten from
  slow-stop-chain, and a 500 would break "stop must work"; MAX_WAIT=120 means no deadline
  fully closes it.
- Under-lock re-check residual: a send that has not yet acquired the lock (or is unconfirmed)
  can still park a message just after the chord wins the lock -- the same capture-window class
  the pi and codex plans accept; visible in the transcript, never silent loss.
- Short-turn corner (pi plan posture): if observe never reported RUNNING, the WAITING->WAITING
  observe event triggers no recompute and the indicator waits for the next transcript event; a
  stop-worthy turn has virtually always been seen RUNNING.
- The abort-evidence signatures are pinned from the tap plan's gate traces; a claude version
  bump could change them -> the fallback restart fires -- visible and safe, never silent.

## Non-goals

- Contracts A and C for claude (sibling plans); the pi and codex stop paths (untouched).
- Removing the base restart-drain (it keeps the nonempty branch and future harnesses).
- Any frontend, wire-format, queue-tracker, or mngr change; per-message withdrawal.
