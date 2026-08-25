# Antigravity (agy) — bringing the chat lifecycle to parity

Status: **plan, nothing built.** Implements
`system/apps/system_interface/imbue/system_interface/harnesses/core-contracts/messages-lifecycle-contract.md` for the `antigravity` harness.
The contract is canonical; where this disagrees with it, this is wrong.

---

## 1. Where agy already is

Working, and matched to claude's shapes:

| Surface | State |
|---|---|
| Launch, sign-in gate, binary pin | done |
| Transcript (protobuf SQLite store) | done |
| Activity indicator + tool captions | done — ladder is claude's, rung for rung, plus one inserted rung |
| Model bar | done — display-only, static catalog |
| Permission cards, tk hiding, output truncation | done — shared `tool_output` helpers |

Not working: **the entire message lifecycle.** Sends resolve on acknowledgement
rather than commit; nothing is ever queued; stop SIGKILLs the agent and every
parked message dies unreturned.

The visible symptom: send "beep" then "bop" to a busy agy. Both arrive, agy
merges them into one turn, and one bubble is left saying "Sending…" forever.
It is not a stuck bubble but a **permanent off-by-N skew that grows** — the
frontend drops one bubble per new arrival id, and a merged turn is one id.

---

## 2. The decision everything else follows from

**We hold the queue. agy never parks anything.**

While agy is busy, messages sit on our side. The moment it goes idle we send
them as one `\n`-joined block. agy therefore only ever receives messages it can
act on immediately.

### Why this, and not a mirror of agy's own queue

agy's constraints are unlike the other three harnesses:

- It accepts input mid-turn only by parking it in the TUI, where it is
  **invisible on disk** — there is no queue to mirror.
- It **merges all parked messages into one turn**, so one committed turn would
  have to discharge N messages.
- Its transcript carries **no client id**, so a merged turn cannot be resolved
  back to the messages that produced it except by matching text.

Mirroring that would mean reconstructing a hidden queue by guessing at text —
and the guess can be fooled by a byte-identical message typed into agy's own
terminal.

By never letting agy park anything, the queue we display becomes the only queue
that exists. One block in, one turn out: delivery is 1:1 and no content is ever
matched.

### What that deletes

| Problem | Why it disappears |
|---|---|
| Matching a merged turn back to N messages | We send one block; it becomes one turn |
| A byte-identical message stealing an entry | Nothing is matched by content |
| Detecting that a merge happened | We do the joining, so we know what is in it |

Two things it does NOT delete, contrary to an earlier draft:

- **The outbox file.** Still required, to survive a `system_interface` restart — see §3.
- **The restart on stop.** Still required as the bounded hammer, because the escape
  chord contends for the same lock a send holds — see §5.

---

## 3. The queue's lifetime: ephemeral, but NOT in-memory

The contract draws a line that is easy to misread, and a first draft of this plan
misread it. Both halves are requirements:

> A Queued message ... is eventually either Delivered (flushed, or consumed at the
> harness's delivery point) or Returned (interrupt). **Never silently dropped while
> the session lives.**
> ... It is NEVER persisted across a session/agent restart, NEVER revived, and NEVER
> auto-sent on resume.

- **Must survive a `system_interface` restart.** A supervisord bounce, deploy or
  crash does not kill the session — agy's tmux session keeps running — so dropping
  the queue there is "silently dropped while the session lives". claude and pi both
  clear this bar because their queues are re-derived from disk (claude from the
  harness's own ledger, pi from mngr's `pi_inbox`). A purely in-memory list does not.
- **Must NOT survive the session.** When agy itself restarts, the queue is gone —
  never replayed, never delivered.

So the store is a file in the **agent state dir**, invalidated by the session's own
identity: the `antigravity_process_started` marker mngr stamps on every launch and
resume. Entries older than the current marker belong to a dead session and are
discarded on load rather than replayed. That is what makes it ephemeral in the sense
the contract means — bound to the session's life, not to our process's.

This is PR 385's `agy_outbox`, restored. An earlier draft deleted it citing
"no durable queue file that outlives the session" — but that clause forbids
outliving the **session**, and the file above does not.

## 4. Phase 0 — the queue

**Store:** an append-only outbox file in the agent state dir (§3), plus the shared
`QueuedSet` in memory as the working copy. The watcher owns it, mirroring pi.

**The busy predicate — stated explicitly, because a first draft did not.** The
`active` marker mngr's statusline maintains, read **under the same `message.lock`
the send takes**. Not the activity indicator: that is derived display state with
staleness and tail-shape rungs, and it can read IDLE mid-turn on a settled planner
step carrying narrative text. The marker is agy's own `agent_state`, verified to
stay `working` continuously across a whole turn including subagents.

Reading it under the lock is what closes the two-web-sends race (§9): the lock is
already held across mngr's entire send, and agy's send does not return until that
marker advances, so a second send that takes the lock necessarily sees the first
one's effect.

**Three transitions:**

| When | What happens |
|---|---|
| Send arrives, marker present (busy) | append to the outbox, publish the list, **do not type** |
| Send arrives, marker absent (idle) | send straight through; nothing is queued |
| Falling edge of the marker | flush (below) |

### The flush

Triggered by the marker's **falling edge**, on a **dedicated per-agent worker
thread**. Never on the watcher thread, and never from the shared working->idle
handler.

That handler is not usable here, and a first draft of this plan wrongly proposed
it. Three reasons, each independently fatal:

1. It is invoked **synchronously** on whichever thread drove the recompute —
   normally the agy watcher thread. A send from there blocks on mngr's strict path
   (blocking `flock`, TUI-ready wait, confirmation bounded by
   `DEFAULT_CONFIRMATION_TIMEOUT_SECONDS = 90.0`). That freezes transcript parsing
   and the activity indicator for up to 90 seconds, on the happy path.
2. It receives **no lifecycle argument**, and IDLE includes *dead-lifecycle* IDLE.
   It cannot tell "turn finished" from "process died".
3. mngr's text send **auto-starts a stopped agent** (`is_start_desired=True`). A
   handler that cannot see a dead lifecycle would therefore resurrect a stopped
   agent and deliver its queue — exactly what the contract forbids ("NEVER
   auto-sent on resume", "the queue is empty whenever the agent is stopped").

So the worker thread checks the lifecycle explicitly and **refuses to flush unless
the agent is alive**. A stopped agent's queue is dropped, never delivered.

**During the flush**, the entries stay on screen marked `is_sending=True` — the
field already exists and already ships for codex. Blanking them and re-showing the
turn would reproduce contract E1 (chips blinking out with no cover); showing the
turn first would invert A3b. The flush is also recorded in the `SendingRegistry`,
so `is_tap_available` and stop's `in_flight_block` can both see it. Without that
the tap is available *mid-flush* and fires a second delivery.

**After the flush**, entries are resolved against the committed transcript rather
than against the send returning — see §10 on why the send returning is not proof.

## 5. Phase 2 — stop

Escape chord to end the turn, then return to the composer: **our queued list, plus
any in-flight block**, in send order, prepended.

Folding `in_flight_block` is not optional. The contract requires returning "every
Queued message **and every in-flight Sending message that has not committed**", and
the base restart-drain explicitly discards it.

**The restart stays as the bounded hammer.** An earlier draft deleted it as claude's
"most awkward limb". That was wrong: the chord takes the same blocking `message.lock`
a send holds, so a chord-only stop can block behind an in-flight send — including
the flush this plan introduces. `restart_drain_under_message_lock` waits 2s and then
hammers regardless, which is the only bounded exit and the only way A5 ("interrupt
must feel immediate") survives. Stop uses the chord on the fast path and falls back
to the restart when the lock cannot be taken.

agy does still delete one of claude's limbs: no idle patch-up, because agy's
statusline clears its own marker on the idle edge.

## 6. Phase 3 — shoulder tap

Escape, then flush the block immediately — the same flush path as §4, so it inherits
the `is_sending` visibility and the `SendingRegistry` record for free.

Gates: nothing in flight, queue non-empty, a turn actually open.

Requires setting `shoulder_tap_class` and flipping
`native_atomic_shoulder_tap_possible`, which is currently False — today the endpoint
400s for agy. Note the legacy `/flush-queue` endpoint has **no frontend caller** and
calls `restart_drain`; it is not the path being built here.

## 7. Phase 1 — the escape keybinding

Provision a rare chord bound to `cli.escape`, the way mngr provisions claude's
`meta+q` → `chat:cancel`.

Shipped as a **single `ctrl+c`**, not `esc`. Both are bound to agy's `cli.escape`, and the
choice is between two different hazards:

- `esc` carries text-editing meaning in too many contexts (menus, dialogs, vim normal mode)
  to deliver blind.
- `ctrl+c` is unambiguous on the FIRST press, but agy treats a DOUBLE press as exit, and its
  docs say that valve fires regardless of remapping.

The second hazard is the controllable one: every caller presses exactly once and falls back to
the restart rather than pressing again. The first is not controllable, because we cannot know
what agy is currently showing.

A dedicated chord bound to `cli.escape` (claude's approach) removes both hazards and remains
the right end state. It is not shipped because agy writes no `keybindings.json` until asked,
and a write to the wrong path would silently no-op and leave stop and tap dead. `cancel_chord`
on the harness spec makes that a one-line change once the path is confirmed.

A dedicated chord means a delivered key can only mean "cancel".

Small shared cleanup: the cancel chord is currently a claude constant imported
into a harness-neutral endpoint. It moves onto the harness spec as a field.

---

## 8. Phase 4 — proof

A conservation storm test, with **interrupt-during-flush as a required case**.

One thing to size honestly: the existing storms drive the real executors and
simulate the harness by replaying the real bytes the executor wrote — claude writes
JSONL, pi writes `pi_inbox`, codex writes a control file. **agy's executor writes
tmux keystrokes**, so there are no bytes to replay and the only durable record is
a protobuf SQLite store agy itself writes. agy's storm therefore needs a different
simulation seam than the other three. This is the largest unestimated item in the
plan.

Also: add agy to the contract document. It currently scopes to "claude, codex, pi"
(Part C), and Part E needs the entries in §10.

## 9. Edge cases

| Edge | Resolution |
|---|---|
| Two web sends in quick succession | Safe. mngr serializes sends on a per-agent `message.lock`, and agy's send does not return until its busy marker advances, so send 1 completing *is* the agent becoming busy. **Requirement: read the busy marker UNDER that lock**, never before it. |
| A terminal send, then a web send, inside the marker's write latency | **Open, accepted as rare.** A terminal-typed message never takes the lock, so it cannot serialize against ours. Worst case our message parks in agy and arrives at the end of that turn — late, not lost. |
| Local vs remote host | `_message_lock` is a **no-op when the host is not local**. Every serialization guarantee above is local-only. Recorded rather than solved; agy agents run in-container today. |
| Strict-send timeout on an accepted prompt | mngr's own docs note a model that refuses a prompt (quota exhausted) never enters a busy state, so a strict send times out although the prompt was enqueued. Reporting that as failed and returning it invites a duplicate resend. Prefer leaving it queued and letting reconciliation settle it. |
| Taking the message lock across a delegated send or chord | **Deadlocks the process against itself.** mngr's send and its chord press both take the same `message.lock`, and flock is per open-file-description, so a second exclusive acquire from this process blocks forever. Decide under the lock, act after releasing it -- which is what claude does, and it calls the resulting gap its accepted capture-window residual. |
| The gap between releasing the lock and the send landing | A concurrent send can start a turn, so we can type into an agy that just became busy. The message is NOT lost -- agy parks it and delivers it at that turn's end -- it is shown as sent rather than queued, arriving later than the UI implied. |
| A send arriving during a flush | The list must be held under a lock across capture-and-clear, or a message appended mid-flush is cleared without being sent. |
| Handler re-firing | The stale-queue check is level-triggered, not edge-triggered. The flush must be idempotent and guard against a second entry while one is in progress. |
| Agent stopped with messages queued | Dropped, never delivered. The worker checks the lifecycle explicitly — see §4. |
| User types into agy's terminal | Invisible to us. Harmless for matching, but it does mean the busy marker can reflect a turn we did not cause; the flush simply waits for it to clear. |

## 10. Known limitations to record in Part E

- **Delivery is not id-matched, and the send returning is not commit.** agy's
  transcript carries no client id, and mngr's send returns when the busy marker
  advances — i.e. when agy became *busy*, not when a user step settled in the store.
  The contract is explicit that an ack is not proof. Mitigation is a reconciliation
  pass after the turn settles; the residual gap (a turn cancelled between ack and
  commit) is real and recorded.
- **A message typed into agy's own terminal is invisible.** Every other harness
  reads a real queue; agy has none to read.
- **The queued list is ours, not agy's.** By never letting agy park anything, ours
  becomes the only queue that exists — but it is a reconstruction, not a mirror, and
  other senders (cron, bootstrap, a telegram bot, the resume prelude) bypass it.
- **Serialization is local-only** (§9).

## 11. Rejected, and why

Recording these so they are not relitigated:

- **Mirror agy's parked block by matching text.** Resolving a merged turn back to N
  messages by joining candidates and comparing. A proof rather than a guess, but
  fooled by a byte-identical terminal-typed message. Superseded by §2 — though see
  the counter-argument in §12, which is the strongest case against this whole plan.
- **A purely in-memory queue.** Violates "never silently dropped while the session
  lives" across a `system_interface` restart. See §3.
- **Flushing from the shared working->idle handler.** Three independent defects,
  each fatal — see §4.
- **Debouncing the queue snapshot ~2s** (PR 385). pi shipped this and deleted it as
  an A2/A3b violation: it delays a real state and lets "Sending…" linger past it.
- **Gating enqueue on the activity indicator.** The indicator is derived state and
  can read IDLE mid-turn. The `active` marker is the ground truth.
- **Greedily marking the agent RUNNING when a message hits the transcript.**
  Contract A6 forbids the overlap it creates. The apparent delay is correct: mngr's
  send does not return until the marker advances, so the indicator lights exactly
  when "Sending…" ends.
- **Deleting the restart hammer.** See §5.

## 12. The strongest case against this plan

Kept deliberately, because it is not settled by writing it down.

Holding the queue converts **read** complexity into **write** complexity, and that
is the worse trade. Mirroring agy's parked block is bounded and read-only: the worst
outcome of a bug is a wrong entry on screen, because agy owns the messages
throughout. Holding the queue makes us the delivery mechanism, and every failure
mode becomes losing or duplicating a real message.

The reason to accept that trade anyway: agy's parked block is genuinely
unobservable, so the read-only design is not "read the queue", it is "guess the
queue from text and hope no two messages are byte-identical". That guess is what
this plan buys out. But the cost is real and the write-side machinery in §4 — worker
thread, lifecycle gate, flush lock, reconciliation — is the price, not an accident.

If that machinery turns out worse in practice than the guess, this is the paragraph
to revisit.

## 13. Done means

- Sending while agy is busy shows a queued entry immediately.
- Stop returns everything unsent to the composer, in order, without restarting.
- Shoulder tap delivers the whole block at once.
- The activity indicator stays exact throughout.
- The conservation storm test is green.

Sequencing: Phase 0 is the prerequisite for Phases 2 and 3, and the only phase
that changes visible behaviour on its own.
