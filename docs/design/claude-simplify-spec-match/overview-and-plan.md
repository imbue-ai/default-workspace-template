# Claude message lifecycle: complexity, spec-conformance, and a plan to simplify toward the clean path

Assessed against the canonical contract `docs/design/harness-message-lifecycle-contract.md`
(invariants A1–A6 + Part B per-operation contracts). Cross-referenced with
`docs/design/harness-audit-2026-08-10/findings-and-fix-plan.md` (P1 items) and the codex
app-server migration (`docs/design/codex-app-server-migration/`), which is the "single
in-house path" shape claude should converge on.

Synthesized from five code slices: SEND, QUEUE/watcher, SHOULDER-TAP, INTERRUPT/stop, and
ACTIVITY/markers. All paths are in `system/apps/system_interface/imbue/system_interface/`
(harness code + `server.py` + `agent_manager.py`) and its `frontend/src/`, plus mngr under
`system/vendor/mngr/libs/`.

The one-line verdict: **claude's message lifecycle works in the common case but is a heuristic
reconstruction bolted onto an opaque TUI.** It reconstructs state claude never exposes (the
queue, "sending", delivery, interruption) by screen-scraping the session JSONL, byte-offset
tail probes, filesystem markers, positional FIFO netting, and an in-process timestamp registry.
The frontend fills the remaining gaps with genuine optimism the backend never reports. The
codex app-server path deletes essentially all of this by minting a stable id at send time and
reconciling per id against the committed transcript.

---

## Part 1 — Complexity & conformance overview

### 1.1 Where the complexity lives

Six load-bearing mechanisms exist **only** because claude has no native turn/queue/interrupt
API. Each is listed with its home and why it is fragile.

1. **SIGKILL restart-drain** — `harnesses/interrupt.py:176` `restart_drain` →
   `server.py:728` `_restart_agent_process` runs `mngr start <agent> --restart --no-resume`.
   This is the branch that carries a non-empty queue back to the composer: it captures
   `get_queued_block()` (interrupt.py:191) *before* SIGKILL, restarts into a **fresh** session,
   `settle_activity()`, then `clear_queue()` (interrupt.py:196). It abandons the transcript
   mid-turn, drops claude's real queue, writes **no** `[Request interrupted by user]` marker,
   and pays up to a ~60s subprocess cap. It is the only interrupt branch that leaves no
   interruption marker (A6 gap) and the branch the audit flagged for the missing lock (P1.6,
   now routed under a bounded lock via `tap.py:566` `_drain_to_base_under_message_lock`).

2. **The bounded message lock** — `harnesses/interrupt.py:103` `try_hold_message_lock`
   (non-blocking `fcntl.flock` poll to `STOP_LOCK_WAIT_SECONDS = 2.0`, interrupt.py:50) on the
   same `message.lock` file (`base_agent.py:369` `_message_lock`) that mngr's `send_message`
   holds across the **entire** paste + confirm window (`tui_agent.py:135` `with
   self._message_lock():` wrapping `submit_message_and_confirm`, up to 90s). Acquiring it means
   an in-flight send has durably parked. The 2s wait is the single heuristic knob deciding
   whether an in-flight send is recovered (parked in time) or hammered-and-lost. Note the send
   path itself (`tui_agent.py:113`) still holds the **unbounded** blocking lock, and it is
   LOCAL-only — remote hosts get no lock at all (`base_agent.py:369` yields immediately),
   relying on SSH serialization.

3. **Screen-scrape send** — `tui_utils.py:198` `wait_for_tui_ready` (regex-poll the pane),
   `:232` `wait_for_paste_visible` (fuzzy-match the normalized message tail in the pane),
   `:437` `submit_message_and_confirm` → `:348` `build_confirmation_command` which emits **one
   linear remote bash script**: capture probe baselines → send Enter → loop {check probes;
   re-send Enter at 3/10/30s} → timeout marker. Plus claude's `_build_submission_evidence_probes`
   (`mngr_claude/plugin.py:2574`, ~150 LOC across 2454–2627). This is the Enter-swallowing
   workaround for bracketed paste — the most timing-fragile part of the send.

4. **Epoch scoping** — `harnesses/claude/watcher.py:166` `_is_dead_epoch_enqueue` compares
   claude's ISO enqueue timestamp against the `claude_process_started` marker mtime
   (`:539` `_read_process_epoch_started_at`) to drop a dead process's dangling enqueues so they
   don't re-derive as ghost chips on `--resume` replay (audit P1.9). A two-clock comparison
   (record ISO ts vs filesystem mtime) that hinges on the SessionStart hook touching the marker
   on every startup/resume; conservative KEEP on a missing/unparseable marker.

5. **Evidence probes** — the delivery verdict. `mngr_claude/plugin.py:2462`
   `_ACCEPTED_MESSAGE_TEXT_JQ_FILTER` selects records where `type=="user"` OR
   (`type=="queue-operation"` and `operation=="enqueue"`) — i.e. **COMMIT and ENQUEUE both
   count as "accepted."** `:2519` `_build_content_evidence_probe` baselines a byte size then
   greps the decoded tail. Clever (evidence-based, not ack-based) but heavy, and the
   commit/enqueue conflation means the send result cannot honestly distinguish Delivered from
   Queued.

6. **The heuristic tap + the stop-timestamp registry** — `harnesses/claude/tap.py:382`
   `execute_claude_shoulder_tap` (~9-branch verdict lattice: refresh-first gate → ACTIVE probe
   → PERMISSIONS_WAITING probe → binding-active check → baseline → chord → `watch_for_flush_verdict`
   byte-scrape poll → FLUSHED / NOT_FLUSHED / NEEDS_RECOVERY). On NEEDS_RECOVERY it injects a
   **synthetic `<task-notification>` RECOVERY_MESSAGE** (`tap.py:91`) the user never sent.
   Coordinated against the stop path (which shares the same meta+q chord) via the in-process
   global `_STOP_MONOTONIC_BY_AGENT` dict (`tap.py:117`), a process-scoped, non-persisted
   registry (`_stop_ran_since`, `tap.py:469`).

Plus a **seventh, frontend-side** subsystem that is pure incidental complexity: the optimistic
overlay in `frontend/src/models/OutgoingMessages.ts` (212 LOC) — "the ONE place the frontend
paints optimistic state" (its own module doc, lines 1–26) — positional oldest-first arrival
correlation (`noteBackendArrivals:140`, no content match), a 6s anti-strand fallback timer
(`resolveOutgoing:116`), a separate 20s flush-freeze (`startFlushFreeze:184`), and a
frontend-tracked in-flight set (`registerPendingSend:60` / `hasPendingSends:72`) that greys the
shoulder tap. This exists **only** because the claude backend never owns a "Sending" state:
`server.py:436` calls the base `note_sent_message`, which for claude is a no-op returning `None`
(claude does not override it), so no backend Sending record is ever created — unlike codex/pi.

**Essential vs incidental.** Essential: capture the queued block, branch chord-vs-restart,
clear the queue, settle activity, verify delivery by durable evidence rather than keystroke ack.
Incidental (deleted by the clean path): the Enter-retry ladder, paste-visible scrape, byte-offset
tail probes, the two verdict lattices, the synthetic recovery message, the stop-timestamp
registry, epoch scoping, positional FIFO netting, and the entire frontend optimistic overlay.
LOC concentration: `tap.py` ~755, `watcher.py` ~1420 (only ~150 is queue logic; the rest is an
unrelated transcript locator/body LRU cache + subagent linkage), `tui_utils.py` ~526,
`interrupt.py` ~252, `OutgoingMessages.ts` ~212.

### 1.2 Per-invariant conformance table

Lead items are the biggest, user-flagged violations. C = conformant, P = partial, **V =
VIOLATED**.

| Invariant / op | Verdict | Evidence (file:line) |
|---|---|---|
| **Interrupt §return** (return ALL non-delivered, in send order, prepended) | **V / P** | The user reports this broken today. Queued messages *are* captured and prepended: `interrupt.py:191` `get_queued_block()` before SIGKILL → `MessageInput.ts:258` prepends `block` above draft. **But in-flight *Sending* messages are never returned** — there is no backend Sending record to return (`server.py:436` no-op `note_sent_message`); the stop path never consults the frontend overlay (`MessageInput.ts:239` `handleStopToComposer` ignores `OutgoingMessages.ts`). A send caught mid-flight is hammered by the 2s bounded lock (`interrupt.py:50`) and lost, or restored only by the frontend's own send-failure path which fires **only if the composer is still empty** (`MessageInput.ts:222`). Conservation hole: if the return path fails to place the block, `clear_queue` (`watcher.py:1298`) / `on_idle` (`:1304`) still removes the chip → message in NO state (audit P1.6, contract Part C known gap). |
| **A2 dumb frontend / never optimistic** | **V** | `MessageInput.ts:190` paints an optimistic "Sending…" bubble via `addOutgoing()`; `OutgoingMessages.ts:1` states it "is the ONE place the frontend paints optimistic state… The backend stays fully decoupled; it is never told about this state." Frontend also makes lifecycle decisions locally: `/login`,`/logout` + declined-slash intercepts decided frontend-side (`MessageInput.ts:158-170`); shoulder-tap **availability** computed from the frontend's own promise set `hasPendingSends` (`OutgoingMessages.ts:72`, `QueuedMessageView.ts:144,167`) — A2 says availability is backend-side. Clears composer + localStorage *before* backend confirms (`MessageInput.ts:186`). (Activity axis is clean — see A6.) |
| **A3 queue fidelity (UI queue IS the harness queue)** | P | Not claude's real queue object — a backend **reconstruction** from the session-file `queue-operation` ledger via positional conservation (`queue_tracker.py:75` `consume`; `queued_set.py:79` `resolve_oldest`). Faithful in the common Minds flow, but drifts: `resolve_oldest` pops the FIFO **head** regardless of which id a `remove` names (non-FIFO leave misaligns content); ledger holes (enqueue with no leave after interrupt/SIGKILL/crash) are reconciled only by the coarse `on_idle` clear-all. Satisfies A2 (backend owns it) but not A3's "IS the harness's own queue." Audit P1.9 (ghost on `--resume`) + P1.10 (~60s discovery blind window) are fidelity bugs. |
| **A3b transitions reflected (in→chip; out→removed+new state; never double/stale)** | P | IN→chip within ≤1s poll (`watcher.py:939`, `POLL_INTERVAL_SECONDS=1.0`) — good. OUT→removed is emitted over **two independent channels with no ordering/atomicity**: the committed transcript turn goes out via `_on_events` (events SSE, `watcher.py:1258`) and the chip-removal snapshot via `_broadcast_queue_snapshot_if_changed` (agents WS, `:1263`). Frontend renders the queued group below the transcript keyed on a different id (`ChatPanel.ts:751-754`; chip `queued_id` hash vs transcript `event_id` uuid), so every Queued→Delivered has a transient **double-show** window (chip + turn) or, if the snapshot wins, a transient **gap**. Also the level-triggered idle backstop can clear a chip on a **false IDLE** (`activity_state.derive` case 4: running + assistant tail + no pending tool) while claude still holds the message. |
| **A4 delivery = COMMIT, per-id reconciliation** | **V / P** | **No stable id is minted at send time anywhere on claude's path** — `SendMessageRequest` carries none (`models.py:38`); `Response.ts:686` body has no id; `_queued_id` (`queue_tracker.py:45`) is an explicitly non-correlation sha1 hash. So per-id reconciliation (A4's core mechanism) is impossible; correlation is **positional** (oldest-first) everywhere. Delivery is evidence-based (good: `submit_message_and_confirm` polls durable transcript, not the keystroke) but the filter conflates COMMIT with ENQUEUE (`plugin.py:2462`), so `send_to_agent`→True means "accepted (committed OR merely queued)," and the tap decides FLUSHED from **aggregate** signals (mirror drained AND assistant answer/turn-alive, `tap.py:283,300`), never per id. A message can leave the parked queue without committing and still read FLUSHED. |
| **A6 activity indicator + interruption marker** | P + **V** | **Activity dot: conformant on A2** (100% backend-owned: `AgentManager.ts:291` assigns `agents` wholesale; `ActivityIndicator.ts:115` only maps state→label; no client write to `activity_state`). **On-interrupt clearing: partial** — the restart-drain path calls `reset_activity_state` directly (prompt broadcast), but the empty-queue chord path (`tap.py:696-706`) calls only `mark_idle()` (clears the `active` marker + pokes mngr observe to re-probe) and does NOT broadcast; the dot clears **indirectly** via the observe re-probe, a documented two-hop best-effort chain with a lag fallback (`tap.py:701-705`). **Interruption marker: VIOLATED** — `session_parser.py:467` (`if text and not is_interrupt_sentinel_text(text)`) **deliberately suppresses** `[Request interrupted by user]` from the transcript, so it is never rendered as a marker row (codex, the clean path, does surface it). Intentional (the sentinel would pin the THINKING heuristic; audit P2.14 forbids synthetic chat messages) but a genuine gap against A6 as written. |
| **A5 everything is fast** | P | Common case fast (idle send commits within one 0.5s poll; queue in/out ≤1s; tap bounded at 3s, `tap.py:102`; stop chord watch bounded 8s). But the HTTP send is fully synchronous, worst-case bounded at 30s ready + 15s paste + 90s confirm with Enter retries (`tui_utils.py:39-66`), and the **unbounded** send-path `message.lock` is held for the whole confirm window, so a slow send blocks subsequent sends and is the documented cause of the ~90s stop stall (audit P1.7). The tap chord uses the **blocking** lock (`base_agent.py:698`), not the bounded one — a latent unbounded wait. |
| **A1 conservation** | P | The watcher only ever **clears** chips (`clear_queue` `watcher.py:1298`; `on_idle` `:1304`) — it never returns text. If the interrupt return path fails to place the block, the sweep still removes the chip → silent vanish (the Interrupt-return gap makes the watcher complicit). No end-to-end conservation test exists (contract Part D not implemented). |
| **Send** (Composer→Sending→{Delivered\|Queued\|Returned}, never stuck) | P | Resolution is produced and bounded (idle→commit; busy→enqueue; strict no-evidence within 90s → `raise_for_unconfirmed_submission` `tui_agent.py:171` → 500 → frontend drop+restore). Deviations: Composer→Sending is frontend-owned (A2); the send-endpoint failure path `retract_sent_message` is a no-op for claude (`server.py:446`); no backend Sending record to reconcile. |
| **Queue** (persists across reload, preserves order, eventually Delivered/Returned) | P | Persists (backend-derived, survives reload). Order preserved only while claude's leaves are strictly FIFO. "Eventually Delivered or Returned" leans on the `on_idle` clear-all backstop for dangling enqueues, which is coupled to the heuristic activity derivation. |
| **Shoulder-tap** (deliver all in order, never half-delivers; greyed while any Sending) | P + **V(availability locus)** | Delivery/ordering delegated to claude's native auto-flush; the tap verifies only mirror-drain + a turn ran, not per-id commit (a native drop/reorder is invisible). "Never half-delivers" enforced only coarsely (accepted failure = a visible spurious recovery message or a spurious 500, `tap.py:24-25`). **Availability decided frontend-side** (`hasPendingSends`, A2 violation); claude's executor has no `SEND_IN_FLIGHT` refusal like codex/pi (`model.py:172/303`), and the refresh-first mirror read (`tap.py:411`) is NOT under the lock, so a tap racing a not-yet-parked send reads NOTHING_QUEUED and no-ops. |
| **Interrupt** (stop turn + return all non-delivered) | **V** | See the lead row. Stop-the-turn works; return-all-non-delivered does not cover in-flight Sending, and the non-empty branch uses SIGKILL (no marker, A6). |

---

## Part 2 — Simplification (ranked, concrete)

Prefer the codex/pi "single in-house path" shape throughout. Ranked by leverage.

**S1 — Mint a stable per-message id at send time and thread it to the transcript.**
Add an id to `SendMessageRequest` (`models.py:38`) / the POST body (`Response.ts:686`), carry it
through mngr's send into the session JSONL, and decide delivery per id against the committed
`user` record. This single change is the linchpin: it replaces the positional oldest-first
correlation used across send/queue reconciliation (`OutgoingMessages.ts:140` `noteBackendArrivals`,
`queued_set.py:79` `resolve_oldest`), unlocks real A4 reconciliation, makes Interrupt §return
decidable (not-committed → Returned) instead of guessed, and lets `queued_set.resolve` (the
id-based path at `:90`, currently codex-only and unused by claude) replace `resolve_oldest` —
eliminating the FIFO-order and phantom-alignment assumptions. Directly deletes the need for
epoch scoping (`watcher.py:166`) and much of the `on_idle` backstop reliance.

**S2 — Give the backend a Sending registry; make claude override `note_sent_message`.**
The backend already serializes every send under `message.lock` (`tui_agent.py:135`). Record the
in-flight send text+id there (like codex/pi) and clear it on commit. Then `server.py:436` creates
a real backend Sending record. This is what lets drain-to-composer return a non-committed
in-flight send instead of losing it (the Interrupt §return fix, without racing a 2s lock).
**Per the contract (A2), the frontend KEEPS its optimistic "Sending…" paint** — that is the one
permitted optimism, shown right after the POST before any backend signal. What gets deleted from
`OutgoingMessages.ts` (212 LOC) is the *reconstruction*, not the bubble: the 6s anti-strand
timer, the positional `noteBackendArrivals` correlation, and the `hasPendingSends` availability
input. The Sending paint's **removal** becomes backend-driven and ordered (A2/A3b): it is removed
only once the message's real representation (queued chip or committed transcript turn) has
appeared — real first, then remove "Sending…" — never before. The queue itself is already a real
backend abstraction (`QueuedSet` snapshot broadcast, `watcher.py:1263` → `QueuedMessageView.ts`);
build on it, do not reconstruct it.

**S3 — Converge the shoulder-tap and interrupt on claude's native control, deleting the
heuristic lattices.** The codex/pi flush is ~10 LOC (`codex/model.py:157`, `pi/model.py:289`):
`try_hold_message_lock` (bounded) → write one control line/sentinel → return
`TAPPED`/`NO_OPEN_TURN`/`SEND_IN_FLIGHT`. A patched-binary/extension flush that merges into the
live turn (contract Part C direction) deletes wholesale from `tap.py`: the `TapVerdict` and
`_AbortVerdict` lattices, `read_raw_tail`/`compute_tail_facts` byte-scrape (`:242,263`),
`watch_for_flush_verdict`/`watch_for_abort_verdict`, the synthetic `RECOVERY_MESSAGE` (`:91`,
also an A3b phantom-chip risk + audit P2.14 conflict), and the `_STOP_MONOTONIC_BY_AGENT`
registry (`:117`, incidental coupling from tap and stop sharing one meta+q chord and an
indistinguishable sentinel). It also removes the SIGKILL `--restart --no-resume` hammer
(`server.py:728`) from the return path, giving A6 a uniform interruption marker.

**S4 — Push shoulder-tap availability to the backend.** Give claude's tap a real
`SEND_IN_FLIGHT`-style refusal (mirror `model.py:172/303`) and have the endpoint report
tap-availability, so the frontend stops computing "nothing is Sending" from its own promise set
(`QueuedMessageView.ts:144`). Lets the frontend drop `pendingSendsByAgent` as an availability
input.

**S5 — Distinguish COMMIT from ENQUEUE in the claude evidence filter** (`plugin.py:2462`) rather
than collapsing both into one "accepted" verdict, so the send result reports Delivered vs Queued
honestly instead of the frontend inferring it.

**S6 — Bound/release the send-path lock.** The send holds the **unbounded** `message.lock`
(`base_agent.py:369`) for the full 90s confirm window. Release or bound it after the message is
submitted/enqueued (before the full commit poll) so it stops being the root of the stop stall
(audit P1.7). Use the bounded `try_hold_message_lock` for the tap chord too (`base_agent.py:698`
currently blocks), matching the discipline codex/pi and claude's own stop path already use.

**S7 — Collapse the two-channel Queued→Delivered transition** (`watcher.py:1258` events SSE vs
`:1263` agents WS) so chip-removal and the committed turn carry the same stable id and the
frontend dedups one against the other (removes the transient double-show / gap). This is a direct
consequence of S1.

**S8 — Unify the two stop-settle mechanisms.** Have the empty-queue chord CONFIRMED branch
(`tap.py:696`) call the same `reset_activity_state`/settle callback the restart-drain path uses
(in addition to `mark_idle`), so the dot clears via ONE direct broadcast, not the indirect
observe re-probe with its documented lag fallback (`tap.py:701-705`).

**S9 — Structural cleanups (low risk, do alongside):**
- Extract the ~150-line queue feed (consume loop + epoch scoping + snapshot broadcast) out of
  `watcher.py`'s ~1000-line transcript-cache/subagent-linkage engine into a small collaborator
  fed by the tailer — independently testable.
- `agent_message_lock` (`interrupt.py:83`) is dead in production (tests only) — delete;
  share the `message.lock` filename+flock helper from `imbue.mngr` instead of the copy (audit
  P4.23).
- Fix the stale comment in `agentLiveness.ts:51` ("the optimistic forced-THINKING the send
  applies" — no such code exists; it invites a false A2 concern).
- Centralize the interrupt-sentinel match (`is_interrupt_sentinel_text`, `session_parser.py:71`)
  everywhere it is used (suppression, abort confirmation, tap verdict) + one fixture, so a
  Claude Code wording change fails one obvious test.
- `concatenated_block` (`queued_set.py:112`) joins distinct messages with a single `\n`; use a
  blank-line join so returned messages keep their boundaries in the composer.

---

## Part 3 — Match-to-spec plan (per violated/partial invariant → exact change)

Ordered so each change unblocks the next. File:line + target behavior.

### 3.1 Interrupt §return — return ALL non-delivered (queued + in-flight Sending), send order, prepended (the lead bug)
- **Add the backend Sending registry (S2).** When mngr enters `send_message` under
  `message.lock` (`tui_agent.py:135`), record `{id, text, order}` for the agent; clear it when
  the commit evidence lands. Surface it to `server.py` so `note_sent_message` (`server.py:436`)
  is a real override for claude, not the no-op.
- **In `execute_claude_stop_to_composer` (`tap.py:588`) build the return block from BOTH sources:**
  `get_queued_block()` (`watcher.py:1294`) **and** any recorded in-flight Sending that has not
  committed, concatenated **in send order** (queued first by enqueue order, then the in-flight
  send — or strictly by the minted order once S1 lands). Return that combined block for **every**
  branch, not just the non-empty-queue branch (today the chord path always returns `""`,
  `tap.py:696`).
- **Reconcile per id (A4):** for each id, committed in the durable transcript → Delivered (leave
  it); not committed → include in the returned block. This makes "what returns" decidable rather
  than the current 2s-lock race (`interrupt.py:50`) that hammers-and-loses an in-flight send.
- **Frontend unchanged in shape:** `MessageInput.ts:258` already prepends `block` above the draft;
  it just now receives a complete block. Remove the composer-empty guard dependence
  (`MessageInput.ts:222`) since return no longer relies on the frontend's own overlay.
- **Close the conservation hole:** ensure `clear_queue`/`on_idle` (`watcher.py:1298,1304`) never
  fire before the return block is placed (already partly addressed by
  `_drain_to_base_under_message_lock`, `tap.py:566`).

### 3.2 A2 — frontend dumb / non-optimistic, EXCEPT the one permitted "Sending…"
Per contract A2, "Sending…" is the single optimistic state the frontend may invent (shown right
after the POST). KEEP it. Remove only the *reconstruction* around it, and make its removal
backend-driven and ordered:
- **Keep** the optimistic "Sending…" paint (`MessageInput.ts:190` `addOutgoing()`). **Delete** the
  reconstruction in `OutgoingMessages.ts`: the 6s anti-strand timer (`resolveOutgoing:116`), the
  positional `noteBackendArrivals:140` correlation, the flush-freeze:184.
- **Ordered removal (A2/A3b):** remove "Sending…" only once the message's real representation has
  appeared — the committed transcript turn or its queued chip (the queue is a real backend
  abstraction: the `QueuedSet` snapshot, `watcher.py:1263` → `QueuedMessageView.ts`). Real
  appears first, then "Sending…" is removed — never a gap. Drive this off the backend's real-state
  report (the transcript event / queue snapshot), not a frontend timer.
- Delete frontend availability computation: `hasPendingSends` (`OutgoingMessages.ts:72`) and its
  use in `QueuedMessageView.ts:144,167`. Availability comes from the backend (3.5).
- Move the `/login`,`/logout` + declined-slash intercepts (`MessageInput.ts:158-170`) to the
  backend, or accept them as pure input-shaping (not lifecycle) — at minimum stop them from being
  lifecycle decisions.
- Stop clearing composer/localStorage before backend confirm (`MessageInput.ts:186`) — clear on
  the backend's Sending acknowledgement.

### 3.3 A4 — per-id commit reconciliation
- Mint the id (S1): add to `SendMessageRequest` (`models.py:38`) and the POST body
  (`Response.ts:686`); thread through `agent_manager.send_message_to_agent` (`:529`) and mngr
  into the session JSONL so the committed `user` record carries it.
- Decide delivery per id against the committed `type=="user"` record; split the filter so ENQUEUE
  ≠ COMMIT (`plugin.py:2462`, S5). `send_to_agent`→ reports Delivered vs Queued honestly.
- Replace positional netting with id resolution: `queued_set.resolve` (`:90`) instead of
  `resolve_oldest` (`:79`); drop `_queued_id`-as-key (`queue_tracker.py:45`) in favor of the
  minted id.

### 3.4 A3 / A3b — queue = claude's own queue, transitions reflected
- With minted ids, the reconstruction keys chips on the real id; enqueue/leave reconcile by id,
  removing the FIFO-head assumption (`queued_set.py:79`) and the phantom-alignment guess.
- Collapse/ORDER the two broadcast channels (S7): for a Queued→Delivered leave, the queue update
  (chip removal) is emitted BEFORE the transcript turn appears — never the turn before the chip is
  gone (contract A3b: the transition goes through the departing queue representation). Emit the
  queue-removal before, or atomically with, the transcript event, so `ChatPanel.ts:751-754` never
  double-shows. (Symmetric to the Sending rule: real→real removes the old first; the fake
  "Sending…" placeholder is the one inversion, real-first.)
- Decouple the idle backstop from a false IDLE: only clear a chip on a durable leave record or a
  reconciled-not-present id, not on `activity_state.derive` case 4 (`harnesses/claude/activity_state.py`).
- Epoch scoping (`watcher.py:166`) becomes unnecessary once ids scope replay; delete it.

### 3.5 Shoulder-tap availability + delivery (A2 + Part B)
- Backend returns tap-availability and a `SEND_IN_FLIGHT` refusal (S4) mirroring
  `codex/model.py:172` / `pi/model.py:303`; take the mirror read under the lock so a tap racing a
  not-yet-parked send does not read NOTHING_QUEUED (`tap.py:411`).
- With native flush (S3), verify per-id commit instead of aggregate mirror-drain
  (`tap.py:283,300`); delete the synthetic RECOVERY_MESSAGE (`tap.py:91`).

### 3.6 A6 — activity/markers on stop, EXACT to the harness (no artificial lag, no overlap)
The refined contract A6 requires the activity dot to be EXACT: model generating → dot shown;
model done → dot cleared IMMEDIATELY on the turn-completion signal, no poll/settle/staleness lag
and no lingering; inherent transport latency is fine, artificial lag is not; and no "Sending…"/
"Thinking…" overlap. For claude specifically:
- **Clear immediately on stop.** Unify stop-settle (S8): the chord CONFIRMED branch (`tap.py:696`)
  calls `reset_activity_state` → one direct broadcast (not the two-hop observe re-probe with its
  lag fallback, `tap.py:701-705`), so the dot dies at once; also reset the tracker's cached
  `_has_pending_tool_use`.
- **Kill artificial lag in the derivation.** Audit `activity_state.derive` for any staleness
  window / poll cadence that keeps the dot up after the turn actually ended (the "~2s lingering"
  class the contract forbids); the dot must reflect the real turn state read from the transcript,
  not a timer. If claude's IDLE can only be known via a poll, keep the poll interval tight and do
  not add any post-turn hold.
- **No Sending/Thinking overlap.** "Sending…" is removed when the message commits / the turn
  starts generating (per A2 ordering, chunk 2); THINKING starts from the same turn-start signal —
  so the two never co-exist on one message.
- Interruption marker: render `[Request interrupted by user]` as a dedicated **marker** event
  (not a `user_message`) that the frontend shows as a marker row and that does NOT feed the
  THINKING tail heuristic — reconciling `session_parser.py:467` suppression with A6 (mirror
  codex's synthetic Interrupted. result at `message-renderers.ts:139`).
- Route the non-empty interrupt through the same native chord (S3) so it, too, leaves a marker
  instead of the SIGKILL restart (`server.py:728`) that leaves none.

### 3.7 A5 — bound the locks
- Release/bound the send-path `message.lock` after submit/enqueue (S6, `base_agent.py:369`);
  use `try_hold_message_lock` for the tap chord (`base_agent.py:698`). Removes the ~90s stop
  stall (audit P1.7).

### 3.8 The conservation test (contract Part D — the enforcement to add)
Add one property-style conservation test for claude (`test_*.py`, acceptance-marked). Drive
Send / Queue / Shoulder-tap / Interrupt in randomized interleavings; after **each** step assert:
- every message is in exactly one state and `delivered + queued + sending + returned = total_sent`;
- zero lost, zero ghosts; order preserved; returns prepended in send order;
- every queue add/removal reflected, nothing double-shown or left stale (A3b);
- **Interrupt-during-flush is a required case** (a tap in progress when stop fires: each
  not-committed message returns in send order on top; each committed stays Delivered).
Claude conforms only when this test is green.

### Sequenced order of changes
1. **S1** mint the stable id (foundation for everything). 2. **S5** split COMMIT/ENQUEUE in the
evidence filter. 3. **S2** backend Sending registry + claude `note_sent_message` override.
4. **3.1** Interrupt §return from both sources, reconciled per id (the flagged bug).
5. **A2 cleanup (3.2)** delete the frontend overlay + availability computation.
6. **S4** backend tap-availability + `SEND_IN_FLIGHT`. 7. **S3** native flush/interrupt path,
delete the verdict lattices + recovery message + stop registry + SIGKILL return branch.
8. **S7/3.4** single-channel transition + id-keyed chips; delete epoch scoping. 9. **S8/3.6**
unify stop-settle + interruption marker. 10. **S6/3.7** bound the locks. 11. **3.8** land the
conservation test green. 12. **S9** structural cleanups. Branch rule (audit): commit on
`claude-codex-pi-dwt` / `*-mngr` only; never push `main`.
