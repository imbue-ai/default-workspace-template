Added codex as a peer harness in the workspace chat UI, alongside claude.

- Depends on the `imbue-mngr-pi-coding` mngr harness plugin, installed with the
  system-interface tool so `uv run mngr` parses its agent-type config.
- Chat-agent creation now selects the harness with `mngr create --type <harness>`
  (which resolves `[agent_types.<harness>]` directly) and layers only the `chat` role
  template on top, instead of stacking a per-harness create template ahead of the role.
  A harness's create template held nothing but `type`, so it was redundant with `--type`;
  dropping it removes one config block per harness with no behavior change.

- Codex transcript tool blocks and the live activity caption now describe what
  the code-mode call actually did -- "Running <cmd>", "Editing <file>",
  "Searching the web" -- instead of an opaque "Tool: exec", from a single shared
  label (so header and caption never disagree).
- The bottom activity indicator is driven by codex's own turn markers
  (task_started / task_complete), so "Thinking..." brackets the real turn; codex
  tool captions are briefly debounced so fast code-mode calls don't flicker.
- Sending to a cold codex agent no longer hangs the request past the proxy
  timeout ("Failed to send: null"): the send is bounded and, if the agent is
  still starting, accepted for background delivery.
- Every non-claude launcher ("New Codex/Pi Agent") is gated
  behind a single FEATURE_FLAG_ENABLE_OTHER_HARNESSES feature flag (off by
  default), so the alt harnesses can be dark-launched and enabled per host without
  a rebuild. (Generalizes the earlier codex-only FEATURE_FLAG_ENABLE_CODEX flag.)
- Creating an alt-harness agent now runs that harness's own CLI sign-in check
  first (codex/pi) and refuses the create with a readable message
  when the harness is signed out, so a chat that could never take a turn is never
  launched.
- Fixed: a codex agent interrupted mid-turn and resumed could stay stuck on
  "Thinking..." until the user sent another message. The restart-boundary check
  looked for claude's marker file on every agent, so it never fired for codex
  (which writes its own).

- pi's tool labels now name the `pi-web-access` extension's tools instead of
  showing a raw `Tool: web_search` / "Running ...". The four tools read like
  their claude/codex peers -- `web_search` -> "Tool: WebSearch" / "Searching the
  web <query>", `fetch_content` -> "Tool: WebFetch" / "Fetching page <url>",
  plus "Checking sources <claim>" (`source_check`) and "Retrieving results"
  (`get_search_content`). Names verified live against pi-web-access 0.19.0; they
  are the package defaults, so a `web-search.json` `toolNames` override renames
  them.

- The pi queued-message mirror is now scoped to the live process generation: a
  drained `user_message` only pops a queued entry when its timestamp is at or
  after the `pi_process_started` marker's mtime, so a dead generation's drains
  (replayed from the durable session file after a backend restart) can no longer
  eat current-generation entries or leave phantom "queued" residue. Pairs with
  the mngr-side pi extension change that archives `pi_inbox` to
  `pi_inbox_history` and truncates it at load, which generation-scopes the
  enqueue side by construction.

- The stop button now interrupts pi natively instead of SIGKILL-restarting it.
  Each harness registers its own stop-button (interrupt-to-composer) behavior:
  claude and codex keep the base restart-drain, while pi appends a
  `{"minds_interrupt_retract": true}` sentinel to `pi_inbox` -- the retract
  sibling of the shoulder-tap flush -- so the running turn is interrupted and its
  queued messages handed back to the composer with no process restart, no
  session-resume cost, and no abandoned-transcript patch-up. It is backend-only:
  the frontend keeps one stop button and one endpoint.

- The stop button now works even when nothing is queued: the empty-queue no-op
  moved off the shared restart-drain and onto the shoulder-tap flush only (a
  flush with nothing to resend is still a no-op), so a stop mid-turn with an empty
  queue now interrupts the turn (claude/codex restart; pi writes the retract
  sentinel) instead of silently doing nothing.

- The pi queue mirror now treats a flush or retract sentinel line in `pi_inbox`
  as a positional clear of the tracked queue, so the visible queue empties
  consistently across a backend restart (every message before the sentinel was
  committed or discarded in the live session) -- repairing both the flush
  phantom-residue case and any risk of a retract resurrecting discarded messages.

- The composer no longer drops the handed-back messages when a draft is already
  typed: a stop prepends the reclaimed block above the existing draft (block,
  blank line, draft) rather than discarding it, closing the only path where
  retracted messages could be lost outright.

The claude "Shoulder tap" now flushes queued messages into the live turn natively, instead
of the SIGKILL-restart-and-resend it fell back to before. It cancels claude's running turn
via a Chat-only meta+q -> chat:cancel chord (provisioned by mngr), which makes claude commit
its parked queue as a fresh merged turn -- the same auto-flush it does at natural turn end.
The keypress is delivered through mngr's in-process message API (holding the per-agent message
lock, so it never interleaves with a text send); dwt never drives raw tmux. A short bounded
watch confirms the flush went through and, in the rare race where the chord also cancels the
just-flushed follow-on turn, sends one recovery nudge so the committed messages are still
addressed. The agent is never restarted and the queue mirror is never torn down. The tap
button now also releases the "Sending queued messages..." freeze immediately on a terminal
no-op (nothing was queued, or no turn was running) instead of holding it to the fallback cap.

- The stop button now interrupts codex natively instead of SIGKILL-restarting it,
  matching pi. codex registers its own stop-button override: with a turn running,
  it appends a `{"retract_turn_id": "<id>"}` line to the same
  `shoulder_tap_atomic.jsonl` control file the shoulder-tap flush writes -- a
  distinct key from the flush's `target_turn_id`, so the patched binary aborts that
  exact turn (ABA-gated on the id) and discards its parked steers while Minds hands
  the queued messages back to the composer, with no process restart, no
  session-resume cost, and no abandoned-transcript patch-up. With no turn running
  it writes nothing and returns an empty block (the parked steers commit on their
  own); with a turn running but an empty queue it still writes the line, so a stop
  mid-turn with nothing queued is a clean turn abort rather than a no-op. Backend
  only: the frontend keeps one stop button and one endpoint. Requires the rebuilt
  codex binary that reads the retract line; an older binary safely skips the
  unknown key.

- The stop button now interrupts claude natively when nothing is queued, instead
  of SIGKILL-restarting it. claude registers its own stop-button override that
  branches on the queue: with an EMPTY queue mid-turn it delivers the same
  Chat-only meta+q cancel chord the shoulder tap uses (through mngr's locked
  keypress; dwt never drives raw tmux), confirms the abort by the interrupt
  sentinel appearing (both the plain and the mid-tool "for tool use" shapes,
  matched on the parsed user record so a tool_result quoting the text cannot false
  confirm), then marks the agent idle so the activity indicator drops immediately
  rather than lying for ~60s (claude fires no hook on a user interrupt). With a
  NONEMPTY queue -- or a permission dialog, an inactive binding, or an unconfirmed
  abort -- it falls back to the base restart-drain, which both interrupts and hands
  the queued messages back to the composer. A stop pressed while a shoulder tap is
  watching the same agent now suppresses that tap's recovery resend, so the tap can
  no longer re-drive the just-stopped turn. Backend only: one stop button, one
  endpoint, no rebuild.

- Fixed: interrupting claude while a tool was running left a phantom "[Request
  interrupted by user for tool use]" bubble in the chat. The transcript parser now
  suppresses that mid-tool interrupt sentinel alongside the plain one.

- The stop button now returns an in-flight message caught mid-send to the composer
  instead of losing it. Every message carries a stable send-time id, and the backend
  now tracks a "Sending" state (accepted, in flight, not yet committed or queued) on
  the claude watcher for the duration of the send. When a stop fires while a send is
  still in flight -- the message lock held past the bounded wait -- claude's interrupt
  now folds that not-yet-committed message into the returned block alongside the queued
  messages, in send order, on every interrupt branch (the empty-queue chord path used to
  return nothing). A send that has already committed or queued is left as-is (reconciled
  per id), so a delivered message is never double-returned.

- The chat composer is now dumb about message state except for the one optimistic
  "Sending..." bubble it is allowed to paint the instant a message is POSTed. Its
  removal is backend-driven and ordered: the bubble drops only once the message's real
  representation (its queued chip or its committed transcript turn) has appeared, real
  first, so there is never a gap. Removed the frontend reconstruction that used to prop
  this up: the 6-second anti-strand fallback timer, the in-flight send registry, and the
  shoulder-tap "Sending queued messages..." freeze. The queued group now mirrors the
  backend snapshot directly (no local hold or blip cover-up), and the shoulder-tap button
  greys only while its own request is in flight -- whether the tap is otherwise available
  is a backend decision, no longer guessed from a frontend promise set.

- The claude "Shoulder tap" now takes its refresh-first queue read under the same
  per-agent message lock a send holds -- the discipline codex and pi already use. If a
  message send is still in flight past the bounded wait, the tap is refused with an
  explicit, retryable "a message send is in flight; try again" instead of reading an
  empty queue and quietly doing nothing, which used to let a tap racing a not-yet-parked
  send miss that message. Backend only: the composer stops guessing tap availability from
  its own promise set and simply surfaces the backend's refusal.

- Fixed a brief double-show when a queued claude message commits: the chat used to
  broadcast the committed transcript turn before removing the queued chip, so for one
  frame the same message could appear as both a chip and a turn. The claude watcher now
  emits the queue update (chip removal) BEFORE the transcript turn within the same poll,
  so a message leaving the queue always vanishes as a chip first and only then appears as
  a turn.

- The claude stop button now clears the "Thinking..." indicator immediately on a
  confirmed native interrupt. On the empty-queue chord path it now runs the same direct
  activity-settle broadcast the restart path uses, in addition to clearing the on-disk
  turn marker, so the dot dies at once instead of lingering until an indirect re-probe
  catches up.

- Added an end-to-end conservation check for claude's message lifecycle. It drives real
  send, queue, shoulder-tap, and interrupt operations in seeded, randomized interleavings
  against the real session watcher and stop/flush executors, and after every step verifies
  that each message is in exactly one live state (Delivered, Queued, Sending, or Returned),
  that nothing is lost or shown in two states at once, that the queue and returned messages
  keep send order, that a message leaving the queue removes its chip before its turn appears,
  and that the "Sending..." record is only cleared once the real state is visible. Interrupt
  fired mid-flush -- with a partially committed queue and with a still-in-flight send -- is a
  covered case. The dead-in-production blocking message-lock helper moved out of the harness
  code into the shared test utilities.
