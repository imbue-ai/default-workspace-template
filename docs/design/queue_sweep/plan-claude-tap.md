# claude shoulder tap: native flush via a dedicated cancel keybinding (no SIGKILL-restart)

## Contract being enforced

Contract C: the shoulder tap commits every queued message into the live session NOW. codex and
pi ship native taps; claude alone still SIGKILL-restarts + resends. Plan: bind `meta+q` to
`chat:cancel` and deliver it externally via `tmux send-keys M-q` -- a Chat-only chord, inert in
every other context, unlike raw Esc (context-aliased to `confirm:no`, `autocomplete:dismiss`,
...). Cancelling the live turn makes claude flush its parked queue through immediately -- the
same auto-flush it performs at natural turn end, which the mirror already tracks -- so the tap
merely triggers that flush early.

**Decision gate: RESOLVED (verified 2026-08-09, claude 2.1.207).** A live scratch agent, a
long generation, two queued messages, `tmux send-keys -t <session>:agent M-q`: the running
turn showed `Interrupted` and BOTH queued messages committed as a fresh merged turn the agent
answered -- the composer was left empty. So `chat:cancel` FLUSHES the queue through (it does
NOT return it to the composer); the plan lands as written for Contract C. The interrupt
sentinel written was the plain `[Request interrupted by user]` (a streaming abort; the
mid-tool variant is the `for tool use` form, per plan-claude-interrupt). The pivot branch in
Open risks is therefore NOT taken.

## Today's behavior and what is wrong

- The claude tap is `_flush_queue_endpoint` (server.py:775-802) -> `_drain_queue`
  (server.py:749-772) -> `mngr start --restart --no-resume` (server.py:711-728): a SIGKILL that
  kills tool subprocesses mid-write, costs a relaunch + resume, and needs `reset_activity_state`
  + `clear_queue` (server.py:770-771) to patch up the state it destroyed.
- The frontend already branches on `native_atomic_shoulder_tap_possible` (QueuedMessageView.ts:36,59);
  claude's catalog sets it False (harnesses/claude/model.py:87). The flush freeze releases on
  transcript arrival (QueuedMessageView.ts:44-54, OutgoingMessages.ts:113-133) -- but a 200
  no-op (codex `no_open_turn`, server.py:868-871) releases nothing: the freeze hangs to the 20s
  cap (OutgoingMessages.ts:145). One small frontend fix below.
- Keybinding provisioning mostly EXISTS: `keybindings.json` is in mngr's per-agent sync set
  (mngr_claude/plugin.py:154, `_sync_user_resources` plugin.py:1276-1289). This workspace runs
  SHARED config mode (`isolate_local_config_dir = false`, .mngr/settings.toml:138): every
  agent's claude reads `/home/user/.claude/keybindings.json` directly. Its `bindings` is a list
  of `{context, bindings}` entries; Chat binds `"escape": "chat:cancel"`; `meta+q` is unbound
  everywhere (verified on this host).

## Minimal change (tear out X, replace with Y)

Tear out: nothing destructively -- `_flush_queue_endpoint` stays as the base path (flag stays,
per decision). The claude tear-out is the frontend routing claude to it (the flag flip).

**mngr side -- provisioning AND the keypress primitive.** The tap's *orchestration* (verdict
vocabulary: queue-operation leaves, interrupt sentinel, live-session resolution) stays dwt
because it reads dwt-only derived state; putting it in mngr would maintain the claude record
signature in two repos. But the *keypress itself* belongs in mngr with the rest of the send
machinery -- dwt NEVER does raw tmux (text sends route through `imbue.mngr.api.message`
`send_message_to_agents`, and mngr owns the flock + tmux; agent_discovery.py:22,193-215). A raw
`tmux send-keys` + `flock` in dwt would duplicate mngr's lock convention in the wrong repo. So:

1. `ensure_chat_cancel_tap_keybinding()` (`libs/mngr_claude/claude_config.py`): merge
   `"meta+q": "chat:cancel"` into the Chat entry of the user-scope `keybindings.json`, creating
   file/entry if absent, via the existing `_claude_config_lock` + `atomic_write` pattern
   (claude_config.py:195-237). Never clobber a `meta+q` already assigned in the Chat or Global
   entry (the only contexts a Chat chord can conflict with); the gate below then reports it
   unavailable. Called from `provision()` alongside `auto_dismiss_claude_dialogs`
   (claude_config.py:449-461). Isolated-mode agents inherit via the existing sync. (NOTE:
   idempotent against an already-present binding -- this workspace's shared keybindings.json
   already carries it from a manual gate test.)
2. `is_tap_binding_active()`: Chat `meta+q` -> `chat:cancel` AND keybindings.json mtime older
   than the `claude_process_started` marker's (no hot-reload assumption).
3. A GENERIC keypress primitive in `imbue/mngr/api/message.py`, beside `send_message_to_agents`:
   "press a raw tmux key token (e.g. `M-q`) in the agent's pane, holding the same per-agent
   `message.lock`" (base_agent.py:368-394,697,709-766 -- the send holds the lock across its
   text+Enter, so an unserialized chord could split a half-delivered message). It presses
   whatever token it is given; the CHOICE of `M-q` is the dwt caller's. mngr changelog entry.

**dwt side (`system/apps/system_interface`):**

3. Add a claude arm to `_shoulder_tap_atomic_endpoint`'s existing inline harness `if`
   (server.py:850-885); the pi and codex arms stay byte-for-byte untouched (the per-harness
   registry treatment is plan-pi-interrupt.md's `/drain-to-composer` piece; if this endpoint
   deserves it, that refactor rides there -- not this plan).
4. New `harnesses/claude/` tap module, invoked in-process (dwt imports mngr_claude directly,
   claude/model.py:29; sends are in-process too, agent_discovery.py:193-232):
   - Refresh-first (codex sibling's posture, plan-codex-interrupt.md:71-77):
     `watcher.get_all_events()` drives `_ensure_cache_current`, the single queue-feed point
     (watcher.py:341,499-516), so a queue that already flushed at natural turn end drains the
     mirror NOW. Then gate: empty mirror -> `nothing_queued` no-op; `active` marker absent
     (activity_state.py:172) -> `no_open_turn`, no chord; `permissions_waiting` present (mngr's
     dialog marker, claude_config.py:823-852) -> 409 "waiting on a dialog" -- the Chat-only
     chord is inert in dialog contexts by design, so fail fast instead of a guaranteed 3s hang;
     `is_tap_binding_active()` false -> 500 `binding_not_active`.
   - Resolve the live session file from the watcher's own state (latest main session,
     watcher.py:981-1021, via the `_find_session_file` walk, watcher.py:1091-1099 -- small
     accessor; NOT the `encode_claude_project_dir_name` path, whose divergence risk its own
     docstring documents, mngr claude_config.py:505-520). Record its byte size as baseline.
   - Deliver ONE `M-q` (ESC+q, one pty write) by CALLING the mngr keypress primitive (piece 3
     above) through the same in-process boundary dwt uses for text sends -- add a dwt method
     mirroring `send_message_to_agent` (on `AgentManager` / `MngrMessenger`) that routes to it,
     so the executor and its tests inject a fake exactly like the text-send fake in testing.py.
     NOT raw `tmux send-keys` / `flock` from dwt: mngr owns the lock + tmux, dwt only decides
     when to press. The lock matters because a send is text plus a SEPARATE Enter, so an
     unserialized chord could split a half-delivered message.
   - Watch (poll ~200ms, <=3s): each poll re-calls `get_all_events()` and classifies the RAW
     post-baseline tail (`parse_queue_signals` session_parser.py:646, `_LEAVE_OPERATIONS` :608,
     `_INTERRUPT_SENTINEL_TEXT` :61 -- raw, since parsed events drop the sentinel, :448).
     Verdicts key on the MIRROR, never on leaves inside the tail: in the designed-for race
     claude's leaves land BEFORE the baseline (the mirror lags its 1s poll, watcher_common.py:26;
     the `active` marker survives the Stop hook's >=3s grace, wait_for_stop_hook.sh), so
     requiring in-tail leaves would misread the race as failure. `FLUSHED` -> `tapped`: mirror
     drained, no post-baseline sentinel lacking an assistant record after it. `NEEDS_RECOVERY`:
     mirror drained + a post-baseline sentinel with no assistant record after it (the chord
     cancelled the flushed follow-on turn). `NOT_FLUSHED` -> 500: deadline, mirror still
     non-empty. Finalized against the gate session's observed trace.
   - `NEEDS_RECOVERY` -> send ONE recovery message via the existing confirmed path
     (`agent_manager.send_message_to_agent`, agent_manager.py:528-536), then `tapped`:
     `<task-notification>Queued messages above were delivered but their turn was interrupted; please address them now.</task-notification>`
     -- the existing injected-kind family: chip-rendered (message-classification.ts:67-81),
     phantom if it ever parks (queue_tracker.py:55-57). The recovery turn's lifecycle re-drives
     the `active` marker whichever way the race left it.
   - No `clear_queue`, no `reset_activity_state`, no tracker change, no restart fallback, no
     backend in-flight set: the frontend `inFlightAgentIds` disable is the series' double-fire
     posture (QueuedMessageView.ts:30,41-42; plan-pi-interrupt.md:90-91), and the refresh-first
     no-op absorbs stragglers.
5. Flip `native_atomic_shoulder_tap_possible=True` (claude/model.py:87). The claude arm calls
   the idempotent mngr ensure function before the gate, so upgraded workspaces self-provision on
   first tap; processes launched before the write fail the gate until their next natural restart
   (pi's takes-effect-at-next-launch posture, plan-pi-queue.md:77-79).
6. Frontend, one small fix: `shoulderTapAtomic` (Response.ts:733) returns the response status;
   `flushQueuedMessages` releases the flush freeze on terminal no-op statuses (`no_open_turn`,
   `nothing_queued`) instead of holding to the 20s cap -- also covers codex's inherited
   `no_open_turn`. Errors already release via the catch (QueuedMessageView.ts:60-66).

## Ship mechanics (which repo/branch/rebuild)

- mngr (claude_config.py ensure + predicate, provision hook, changelog): vendored tree
  `system/vendor/mngr/libs/mngr_claude/` -- lands on **claude-codex-pi-mngr**.
- dwt (server.py claude arm, harnesses/claude tap module + watcher accessor, model.py flag,
  Response.ts/QueuedMessageView.ts no-op release, tests): lands on **claude-codex-pi-dwt**.
  No codex fork change, no rebuild, no sha256 repin, no pi change.

## Tests

- mngr `claude_config_test.py`: merge into an existing Chat entry; creates file/entry when
  absent; leaves an existing Chat/Global `meta+q` untouched (an unrelated context's `meta+q`
  does NOT block); idempotent; predicate mtime-vs-marker ordering.
- dwt tap module tests: verdict lattice over synthetic raw tails + mirror states, including the
  pre-baseline-leaves race (drained mirror + sentinel-only tail = NEEDS_RECOVERY, not failure)
  and its idle-gap variant (drained mirror, no sentinel = FLUSHED); every gate; the chord holds
  `message.lock`; target construction. The live chord itself is verified manually via tmux
  (repo convention: not crystallized).
- dwt `server_test.py`: claude tap never restarts and never clears the mirror; status mapping
  (no-ops, 409, 500); pi and codex branches untouched (existing tests pass unchanged).
- frontend: freeze released on a no-op status; still arrival-released on `tapped`.

## Open risks

- **Pivot:** if the decision gate shows cancel RETURNS the queue to claude's composer, the chord
  serves Contract B: this plan does not land; claude keeps restart-flush (flag stays False) and
  the chord becomes claude's native `/drain-to-composer` override. One decision, no dual design.
- The race signature is asserted from one observed trace; a claude version bump can silently
  change it. Failure mode: a spurious recovery message or a spurious 500 -- visible, never loss.
  Likewise 3s can expire before records land on a loaded host -> `NOT_FLUSHED` 500 while the
  flush arrives anyway: an error popup and a truthful mirror; nothing was resent.
- Stop button during the 3s watch: without a guard the stop matches NEEDS_RECOVERY's exact
  signature (mirror drained + post-baseline sentinel) and the recovery send would re-drive the
  just-stopped messages. plan-claude-interrupt amends this module: the stop override records
  an in-process per-agent stop timestamp and the recovery arm suppresses its send when a stop
  ran since the tap's baseline.
- ESC+q rides one pty write, but if the reader ever splits the bytes while a Confirmation dialog
  is up, the lone ESC is `confirm:no` -- the wrong-opcode hazard reappearing via byte-splitting;
  the `permissions_waiting` gate keeps the tap out of the known dialog states.
- The mtime gate treats a keybindings.json edited after claude launched as not-active (a
  conservative false negative); heals on the next restart.

## Non-goals

- Contracts A and B for claude (sibling plans; Contract B is plan-claude-interrupt: the chord
  serves the empty-queue stop, the restart-drain base keeps the nonempty branch).
- Removing `native_atomic_shoulder_tap_possible` or `/flush-queue` (base path stays -- user
  decision).
- The codex and pi tap paths: untouched, not even relocated (any per-harness registry for the
  tap endpoint rides the interrupt plans' refactor). No keybinding UI, no queue tracker change.
