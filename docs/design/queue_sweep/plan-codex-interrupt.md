# codex stop button: native interrupt-and-retract (no SIGKILL-restart)

## Contract being enforced
Contract B: the Minds stop button interrupts the running turn, returns the queued messages to
the user's composer, and leaves the harness's own queue empty -- without killing the process.
Prerequisite: `plan-codex-queue.md` (codex Contract A) lands FIRST -- the handback trusts the
mirror snapshot, so orphaned prior-generation ledger entries must already be scoped out; else
a stale mirror over an idle agent makes the no-open-turn path a permanent no-op on phantoms.

## Today's behavior and what is wrong (precise citations)
- The stop button (`frontend/src/views/MessageInput.ts:238` `handleStopToComposer`, visible
  whenever the agent is working, `:408`) calls `/drain-to-composer`
  (`system/apps/system_interface/imbue/system_interface/server.py:888`), which for EVERY
  harness runs `_drain_queue` (`server.py:749`): capture the mirror block, SIGKILL-restart via
  `mngr start --restart --no-resume` (`server.py:711`), clear the mirror. And empty-queue stop
  is a silent no-op: `_drain_queue` returns before restarting when the block is empty
  (`server.py:762-763`), so stop mid-turn with nothing queued interrupts nothing -- Contract B
  requires a pure turn-abort there.
- The patched binary already has the retract machinery: `on_interrupted_turn` with
  `submit_pending_steers_after_interrupt` false drains parked steers back into codex's own
  composer and appends `queued_retracted` per id (`chatwidget/input_restore.rs` hunk,
  `patches/0.146.0.patch` ~2173-2189). But nothing external can trigger that branch: the
  `shoulder_tap_atomic.jsonl` watcher (`app/shoulder_tap_atomic.rs`) only triggers the FLUSH
  branch (`trigger_shoulder_tap_flush`, `chatwidget/interaction.rs` hunk ~2287).
- Why native rather than fixing the restart path in place: the no-op alone IS fixable in a
  few lines (restart when a turn is open, as `/interrupt` does, `server.py:697-706`), but the
  restart is real collateral -- the SIGKILL kills codex's tool subprocesses mid-write, costs
  seconds of relaunch plus resume replay, and forced the frontend's "pending restart" flash
  protection. The recorded veto of a native interrupt
  (`docs/system/blueprint/agent-liveness-overlay/plan-agent-liveness-overlay.md:151-153`,
  commit c158e443) was against synthesizing an Esc keypress into the TUI; the control-file
  channel used here was approved and shipped AFTER that veto (the atomic shoulder tap,
  `server.py:850` onward). This adds its retract sibling, as `plan-pi-interrupt.md` does on
  pi's inbox channel.

## Minimal change
**codex fork (tear out nothing; extend the existing control channel):**
- Same file, new line shape: a retract intent is `{"retract_turn_id": "<id>"}` appended to the
  existing `shoulder_tap_atomic.jsonl`. NOT a `"mode"` field on the flush line:
  `parse_control_line` tolerates extra fields (asserted by
  `minds_shoulder_tap_atomic_control_line_parses`), so an old binary would run a FLUSH --
  committing the very messages the user asked back. A distinct key makes old binaries skip the
  line as malformed (fail-safe) and reuses the whole tailer/event/serial-stage pipeline; a
  sibling file would duplicate the tailer and lose flush/retract ordering.
- `parse_control_line` returns an intent enum (Flush(id) | Retract(id)); `AppEvent::ShoulderTapAtomic`
  carries it. `decide_shoulder_tap` grows the retract arm: same ABA gate (id mismatch or idle
  -> Ignore) but `has_pending_steers` is NOT required -- a matching live turn with an empty
  queue is a pure turn-abort, exactly what the empty-queue stop needs.
- New `trigger_shoulder_tap_retract` beside `trigger_shoulder_tap_flush`: leave
  `submit_pending_steers_after_interrupt` false, set a one-shot `discard_pending_steers_after_interrupt`
  on the input queue, submit `Op::Interrupt`, `pause_active_goal_for_interrupt`. In
  `on_interrupted_turn`'s composer-restore branch, the flag keeps the per-id `queued_retracted`
  appends but DROPS the drained text. Discard scope, settled now: pending steers, rejected
  steers, AND `queued_user_messages` -- everything the SIGKILL destroys today. This codex is
  headless: restoring ANY of them into its composer would silently prepend to the next message
  mngr types; Minds hands the mirrored text back in its own composer -- one owner, no
  duplicate. Esc/Ctrl-C keep upstream restore behavior.
- The append-only control file stays safe across process restarts with no seeding: the
  re-read from byte 0 is harmless because turn ids never recur -- the ABA gate no-ops every
  historical line.

**Minds side (tear out the codex restart; replace with the pi plan's optimistic
capture-then-retract -- no confirmation machinery):**
- `_drain_to_composer_endpoint` dispatches through the per-harness interrupt-to-composer
  implementation `plan-pi-interrupt.md` introduces (the `switch()` precedent: registered per
  harness, base = the shared restart-drain that claude and any future harness use by
  default; codex registers the native override below). No wire-visible catalog flag: the
  frontend keeps one button and one endpoint, and unlike
  `native_atomic_shoulder_tap_possible` there is nothing to 400-reject -- an unregistered
  harness legitimately falls through to the base restart path. codex override:
  (1) `events = watcher.get_all_events()` -- refreshes and consumes the queued-input sidecar
  (`codex/watcher.py:453-457`; bare `get_queued_block()` does not, `watcher.py:534-541`) so
  the capture that follows is current; (2) capture the block; (3) `current_open_turn_id(events)`
  (`codex/activity_state.py:49`) -- None means nothing is running and any parked steers are
  committing on their own (no orphans once Contract A is in), so return `{block: ""}`
  untouched; (4) append `{"retract_turn_id": <id>}` to the control file (path per
  `server.py:873-875`); (5) `watcher.clear_queue()` (`watcher.py:542`) and return the block.
  No poll, no restart fallback, no `reset_activity_state`: the rollout's `turn_aborted`
  settles activity (`codex/watcher.py:337`) and the `queued_retracted` records also clear the
  mirror via the existing tracker path.
- Optimistic handback is the series-wide race posture (`plan-pi-interrupt.md:86-88`): if the
  turn ends between capture and the tap, codex ABA-ignores the line and the steers commit --
  worst case the same text is in the transcript AND back as an unsent, user-reviewed draft. A
  visible duplicate, never a silent double-commit; the identical window exists in today's
  capture-before-SIGKILL path. So no leave-kind split, no id->resolution map, no
  session_parser/queue_tracker change at all.
- Frontend: no change. `handleStopToComposer` already drops a non-empty `block` into the
  composer and treats empty as a no-op.

## Ship mechanics (which repo/branch/rebuild)
- codex changes land in the codex-in-minds fork (`.external_worktrees/codex-in-minds/`):
  regenerate `patches/0.146.0.patch` + `patches/0.146.0-patch-details.md`, rebuild both arches
  via `./build.sh --version 0.146.0` (the `cargo test -p codex-tui --lib minds_` gate must
  pass), update `SHA256SUMS`, publish a new GitHub release tag.
- dwt changes (server.py + tests only) land on `claude-codex-pi-dwt`, sequenced AFTER
  `plan-codex-queue.md` on the same branch; bump `CODEX_PATCH_RELEASE` (`setup_system.sh:40`)
  and the two `codex_patch_sha256` pins (`setup_system.sh:256-257`).
- No mngr changes; nothing lands on `claude-codex-pi-mngr`.

## Tests
- Patch (in `tui/src/app/shoulder_tap_atomic.rs` + `chatwidget/tests/minds_queue_ledger.rs`):
  `minds_shoulder_tap_retract_control_line_parses` (distinct key; old flush lines unaffected;
  malformed skipped), `minds_shoulder_tap_retract_decision_allows_empty_queue`,
  `minds_shoulder_tap_retract_ignores_mismatched_or_idle_turn`,
  `minds_shoulder_tap_retract_resolves_steers_as_retracted_and_discards` (ledger gets
  `queued_retracted`; pending, rejected, and queued_user_messages all dropped; draft untouched).
- `server_test.py`: codex drain appends the retract line, does NOT restart, clears the mirror,
  and returns the pre-captured block; no open turn returns empty block with no write; empty
  queue with an open turn writes the line (pure abort) and returns empty block; claude/pi
  behavior unchanged (existing `test_drain_to_composer_*` keep passing).

## Open risks
- Version skew (new server, old binary): the old binary skips the unknown line, so the turn
  keeps running while Minds cleared its mirror and handed back the block, and the pure abort
  silently no-ops. Self-heals on the next process start; same accepted posture as pi
  (`plan-pi-interrupt.md:138-140`). No restart fallback: that would wire two interrupt
  mechanisms into one endpoint forever to cover a transient window the series accepts elsewhere.
- A steer sent between capture and the tap is injected then retracted-and-discarded, absent
  from the handback. Same-width window as today's capture-vs-kill; inherited, not introduced.
- Entries invisible to the mirror (a core-bounced steer awaiting auto-resubmit, patch
  ~2141-2148; `queued_user_messages`) are discarded with no handback -- matching what the
  restart destroys today. If the turn instead ends before the tap lands, a bounced steer
  auto-resubmits as a fresh turn: visible-duplicate class again.
- A human attached via `mngr attach` sees externally-retracted steers vanish from codex's UI
  (they reappear in Minds). Scoped to the external trigger only.

## Non-goals
- Contract C, and Contract A for claude/pi -- separate plans (codex Contract A is not a
  non-goal but a declared prerequisite, `plan-codex-queue.md`).
- Native interrupt for claude or pi; claude's restart-based drain stays (pi has its own plan).
- Withdrawing a single queued message; any frontend redesign of queued bubbles; changing
  `/interrupt` (mngr stop) or `/flush-queue`.
