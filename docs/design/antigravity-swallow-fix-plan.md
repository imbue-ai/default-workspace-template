# agy: one typist — the plan

Supersedes `antigravity-turn-open-latch-spec.md` (wrong diagnosis) and the F1–F10 draft of this
file (right diagnosis, a fix that would have been worse than the bug). Reviewed by three
independent passes; every claim below is cited to code or is explicitly marked unverified.

## 0. The bug

The flush worker never asks whether a turn is open.

```python
while not self._stopping.is_set():
    self._flush_wake.wait(timeout=_FLUSH_RETRY_SECONDS)   # 5.0s; return value IGNORED
    self._flush_wake.clear()
    self._attempt_flush()                                  # gates on is_alive() ONLY
```

`Event.wait` returns on timeout as well as on the event, so this is a 5-second unconditional
tick. A message held mid-turn is typed into the live turn within 5s; agy merges it; and mngr's
submission probe is the `active` marker's *mtime*, which `statusline.sh` touches on every busy
sample — so the parked message **reports delivered**, entries resolve, chips vanish.

Two earlier diagnoses were wrong and are retracted: the marker does **not** drop during tool
calls (`statusline.sh`'s header records 75 consecutive busy samples across a ~29s subagent run,
zero mid-turn idle blips, verified against agy 1.0.6/1.0.7), and `_turn_state` is not "read
nowhere" — `session.send` and `tap.py` read it through the registry; only the flush worker
never asks.

## 1. Why the obvious fix is a worse bug

Gating the flush on `is_turn_open` **does not terminate**. `is_turn_open_by_tail` returns True
for a `tool_result` tail, a `user_message` tail, an empty `assistant_message`, or any unmatched
tool call. A cancelled turn leaves exactly that tail and nothing ever closes it. The staleness
gate does not help: a cancel involves no restart, so the tail is younger than
`antigravity_process_started` and reads fresh forever.

Post-gate, **the first stop an agy agent receives permanently disables both typists.** Every
later message is held; the flush never runs; chips accumulate forever. A silent per-agent
outage. The swallow at least delivered the text.

**Measured on agy 1.1.20, not predicted.** A single `ctrl+c` during a tool call leaves:

```
idx=11  STEP_TYPE_132  status=CANCELED  is_terminal=True
```

so the scan cursor advances normally, and the parser *does* emit a `tool_result` — which means
`has_unmatched_tool_use` is **False**. The strand arrives by a different route than expected:
the tail is a `tool_result`, and `is_turn_open_by_tail`'s rung 3 reads a `tool_result` tail as
"agy is about to speak next". Nothing ever follows a cancelled turn, so it is True forever.

`tap.py`'s own docstring already says this, and this author wrote it — applied to
`_wait_for_turn_to_end`, while proposing the opposite for the flush gate:

> if a cancelled tool chain leaves its last tool call unmatched forever, a transcript-based
> wait here would never be satisfied

**Every rung of the hold predicate must be freshness-bounded, or the queue cannot progress.**

## 2. The design

### 2.1 One typist

`session.send` never types. It enqueues, publishes the chip, wakes the worker, returns.

```python
def send(self, text: str, message_id: str) -> SendOutcome:
    self._queue().enqueue(text, _now_iso())   # publishes + wakes inside the tracker lock
    self._deps.notify_agents_changed()
    return SendOutcome.OK
```

This deletes: the hold-vs-type branch, the `try_hold_message_lock` probe, the documented
"residual window, accepted", the flock-per-fd hazard on the send path, and any need for a new
typist lock to arbitrate between two typists that should be one.

`is_sending()` becomes `tracker.is_sending()` and is correct by construction, because the only
in-flight send is a claimed flush.

**Price, stated plainly:** a message to an *idle* agy renders as a queued chip for one flush
cycle instead of going straight to a turn. Contract A2/A3b permit it — Queued is a real
backend-reported state, and the chip appears synchronously inside the POST, which is *better*
A1a than today (today the real representation is a transcript turn up to a second later).

### 2.2 One hold predicate, every rung bounded

The `active` marker's mtime becomes the **primary** busy signal, used only in its trustworthy
"asserts busy" direction. The transcript corroborates during the marker's lag windows. This
inverts `turn_state.py`'s current docstring, and it is right: the marker's premise is verified
live, the docstring's premise is not.

```python
BUSY_ASSERT_SECONDS = 60.0     # the statusline asserted busy this recently
TAIL_OPEN_SECONDS   = 1800.0   # backstop for an open tail with no cancel and no restart

def is_hold_required(state_dir, events, last_cancel_at, process_started_at, now) -> bool:
    marker_at = _mtime(state_dir / ACTIVE_MARKER_FILENAME)
    if marker_at is not None and now - marker_at < BUSY_ASSERT_SECONDS:
        return True                                   # positive evidence of busy
    tail_at = _tail_epoch(events)
    if is_turn_open_by_tail(events) and tail_at is not None:
        if tail_at + 1.0 <= max(process_started_at or 0.0, last_cancel_at):
            return False                              # abandoned by a restart or by OUR cancel
        if now - tail_at < TAIL_OPEN_SECONDS:
            return True
    return False
```

`last_cancel_at` is stamped by stop and tap immediately before pressing — a cancel is the only
in-process cause of an abandoned tail, and we are the ones who cause it, so this is exact
rather than heuristic. The `+ 1.0` covers a real granularity bug: `agy_transcript._iso_timestamp`
has 1-second resolution and is compared against a nanosecond mtime, so a genuinely fresh row
written in the same second as the marker reads as stale — a swallow the naive gate introduces.

Both rungs are bounded, so the predicate is guaranteed to go false within
`max(BUSY_ASSERT, TAIL_OPEN)` of the last observable activity. **Progress is guaranteed.**

### 2.3 Delivery is a content match, not "a turn opened"

"Did a turn open?" cannot tell *whose* turn. A terminal typist or a cron send opening a turn
during our confirm would resolve our chips while our block sits parked.

```
delivered  iff a USER_EXPLICIT row appears after the send whose cleaned text == the block
partial    iff such a row equals a proper prefix of the block on line boundaries
              -> resolve the covered ids, requeue the rest
none       iff neither within ROW_WAIT (15s) -> requeue, attempts += 1
```

`partial` is **defence in depth, not a live failure mode** — measured on agy 1.1.20:
`tmux send-keys -l` with an embedded `\n` *inserts a newline in the composer and does not
submit*. Both lines sat as one draft; a single Enter then committed them as exactly one
`USER_INPUT` row, which agy echoed back verbatim. "One block, one turn" is verified, so E12's
premise holds. Keep the prefix branch because it is four lines and the cost of being wrong is a
silent drop, but build nothing else on the possibility.

At `attempts >= 3`: stop resending, keep the entries visible, surface as failed. Unbounded
retry of an unverifiable delivery is a duplication generator — and each retry is a *fresh*
paste of the whole block.

### 2.4 The tap never sends

`begin_flush()` first (chips read "Sending…", button greys), stamp `last_cancel_at`, press
`C-c` once, wait for the marker, then wake the worker. The worker delivers and settles.

Today the tap reads the block, releases the lock, presses, and waits up to 8s — which is
exactly the idle edge the flush worker is waiting for. The worker can claim and send the same
block during that wait, after which the tap sends it again. One delivery path removes it.

Add a hard interlock: reject a `C-c` within `MIN_PRESS_INTERVAL` (5s) of the previous press.
A double press **exits agy**; that is the only failure here that destroys the agent process,
and it deserves more than a greyed button.

### 2.5 Ownership

One tracker per agent for the agent's life, keyed by agent id alone. `set_session(token)`
clears entries, clears claims, and **deletes the journal** on change — "reset the token in
place" without clearing would carry a queue across a restart and auto-send it, which Part B
forbids outright. A missing marker means *no session*: never journal, never replay (today `""`
matches `""`, so a queue journalled with no marker survives any restart).

Every mutator publishes inside the tracker's own lock, so the snapshot-then-callback race
cannot be written. The worker also publishes on every tick, level-triggered — an untrack /
re-track cycle currently leaves live queued messages with no chips until the next mutation,
which is the forbidden "resurfaces later".

### 2.6 Liveness before holding

`session.send` has no liveness check today. A stale `active` marker on a STOPPED agent means
enqueue → **200 OK, chip shown** → the flush's dead-agent branch calls `clear_queue()` → the
message is destroyed. Not Delivered, not Returned. Send must delegate (mngr auto-starts) rather
than hold when the agent is not alive, and the dead-agent branch must *return* the queue, never
silently clear it.

### 2.7 Clear the `active` marker at launch (mngr-side, one line)

**Nothing clears agy's `active` marker, ever.** Its only writers are `statusline.sh` (touch on
a busy sample, `rm` on the idle edge) and a launch-time touch of the *different*
`antigravity_process_started` marker. `mngr_claude` and `mngr_pi_coding` both explicitly
`rm -f "$MNGR_AGENT_STATE_DIR/active"` at launch; agy does not.

So a SIGKILL mid-turn — stop's restart hammer, `mngr stop`, a container kill — leaves the
marker present **indefinitely, across restarts**. Consequences at exactly the edges this plan
cares about:

- a relaunched agent reads busy from its first instant until agy's first idle sample;
- during that window every send is held (the marker is the primary rung, §2.2);
- and with the dead-agent branch as written, the flush then *destroys* them (§2.6).

The shared docstring at `activity_state.py:170` asserts "the launch-time marker clear covers
the rest". For agy that clear does not exist, and the sentence is load-bearing on the marker arm.

Fix in `mngr_antigravity/plugin.py`, mirroring what pi already does at launch:

```python
f"rm -f {active_marker} 2>/dev/null || true; touch {process_started_marker} 2>/dev/null || true"
```

This makes the stale-marker edge *disappear* rather than be defended against, which is better
than any predicate. It is a vendored-mngr change, so it mirrors out to the mngr PR.

## 3. The state machine

Eight states, not five. S6 and S7 exist in the code today and are unnamed; any document that
omits them is lying about conservation.

| state | where it lives | visible as | absorbing |
|---|---|---|---|
| S0 Composer | frontend | text in input | no |
| S1 Sending | `SendingRegistry` | "Sending…" | no |
| S2 Queued | tracker, id ∉ `_sending_ids` | chip | no |
| S3 Claimed | tracker, id ∈ `_sending_ids` | chip rendered "Sending…" | no |
| S4 Delivered | agy's store, a matching user row | transcript turn | yes |
| S5 Returned | HTTP response → composer | text in input | yes |
| **S6 Merged** | agy's TUI composer → inside someone else's turn | nothing | yes |
| **S7 Discarded** | nowhere (dead agent, token change, untrack) | nothing | yes |

**Conservation as an equality is refuted.** With no id in agy's store, a false-negative send
puts one accepted message into S4 twice. The provable law is a pair:

- **no loss** — every accepted message is in ≥1 state at every instant, and
- **bounded duplication** — extra deliveries are bounded by the attempt ceiling (§2.3).

And instantaneous conservation is not the property we want: a message stranded in S2 forever
satisfies it. **Progress** is the property: every accepted message leaves {S1,S2,S3} within a
bounded time. §2.2 is what makes that true.

## 4. Tests

The storm must drive the real `_attempt_flush`. It currently hand-writes `_AgyWorld.flush()`
and scores a mid-turn flush as a delivery, which is why all of this shipped green.

But driving real code against a lying world validates the wrong gate. `_AgyWorld.begin_tool_call`
encodes the **disproven** marker model (deletes the marker on every tool call) and comments it
as "the production shape". Fix it to match `statusline.sh`, then add a second scripted world for
the disputed model — until the quota returns, "we do not know which is true" must be two worlds,
not one comment.

Also required, none of which the storm can express today:
- a world where a block submits only its first line (§2.3 `partial`)
- an out-of-process actor appending a user row while a flush confirms (§2.3, whose turn?)
- a cancelled-tool-chain world asserting the flush still drains (§1, the strand)
- a `session_token` change mid-claim
- assertions on **progress** and on S6/S7 as their own buckets — the instantaneous equality
  alone would have passed the strand, and a set-based ledger hides duplication

## 5. Doc corrections

- **E11** ("agy is never allowed to park anything") is false — the ungated flush parks messages,
  and the dead-agent path destroys them.
- **E12**'s mechanism is stale (the dot now ignores the marker). Its "one block, one turn" is
  now **verified** (§2.3), so state it as measured rather than asserted.
- **E13** says stop and tap both fall back to a restart; only stop does.
- `registry.py:318` still says "NATIVE Escape" while `cancel_chord` is `C-c`.
- `tap.py`'s `_wait_for_turn_to_end` docstring says a cancelled chain leaves its tool call
  "unmatched forever". Measured, the call *is* matched and the tail is a `tool_result` — the
  conclusion holds, the stated mechanism does not.
- `session.py`'s `in_flight_block` comment is wrong in a direction that invites dropping
  in-flight sends.
- The two docstrings asserting the marker drops during tool calls (`turn_state.py`,
  `activity_state.py`) are load-bearing and false.
- `activity_state.py:170`'s "the launch-time marker clear covers the rest" is false for agy
  until §2.7 lands.

The architecture diagram (conversation-contexts/opencode-agy/artifacts) carries the same two
retracted claims.

## 6. What nobody can stake their life on yet

Quota-blocked (~160h), so all of the above is derived from source. In priority order, these are
what to answer when it returns — not "does the fix work", but:

1. **Does the marker stay present through a long, quiet tool call?** The header's evidence is a
   29s run at ~2.6 samples/s. It does not establish that agy fires `statusLine` during a
   20-minute silent command. `BUSY_ASSERT_SECONDS = 60` is a guess; if agy only fires on state
   change, the marker rung goes cold and the tail rung carries it alone. (The *stale*
   marker half of this question is now moot — §2.7 clears it at launch.)
2. ~~Does a newline-joined block submit as one turn?~~ **ANSWERED** (agy 1.1.20): yes. An
   embedded `\n` inserts a newline and does not submit; one Enter commits the whole block as a
   single `USER_INPUT` row. See §2.3.
3. **Does `_clean_user_text` round-trip our block exactly?** If agy normalizes whitespace or
   wraps, exact matching fails closed → resend → duplicate. Cheap to check against a captured
   `.db`.
4. **Is agy's double-`C-c` valve time-bounded?** `MIN_PRESS_INTERVAL` assumes it is. If any two
   presses in a session exit, the only safe design is one press per process lifetime.
5. **Can agy agents be remote?** mngr's flock is a no-op for non-local hosts, so the in-process
   lock would be the only serialization that exists.
