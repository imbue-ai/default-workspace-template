# agy turn-state: the `active` marker is the wrong signal (SPEC — not implemented)

## 1. The two failures are one bug

Both reported failures come from a single mistake: we treat agy's `active` marker as
"a turn is open", but it actually means "the model is emitting tokens right now".

`statusline.sh` writes the marker from agy's self-reported `agent_state`:

```sh
case "$agent_state" in
    idle | initializing | authenticating | "") rm -f "$marker_file" ;;
    *)                                         touch "$marker_file" ;;
esac
```

and the agy binary contains exactly two such states:

```
idle
thinking
```

There is no `tool_calling`, no `working`, no `executing`. So the marker is up only while
agy is *thinking*, and is removed for every other part of a turn.

### Failure A — the marker is LATE (confirmed live)

Sampled every 0.4s immediately after a send was confirmed submitted:

```
 0.4s  MARKER_DOWN     <- message already submitted, we still read "not busy"
 1.2s  MARKER_UP
```

An ~800ms window where a turn is open and we believe the agent is idle. This is not
cosmetic: `session.send` decides hold-vs-type on exactly this marker, so a second message
arriving in that window is **typed into a busy agy** instead of held. agy then merges it
into the running turn — the precise outcome the whole design exists to prevent.

### Failure B — the marker DROPS mid-turn (mechanism confirmed, live repro blocked)

During a tool call agy is not "thinking", so `agent_state` is `idle` and the marker is
removed while the turn is still open. Downstream: activity derives IDLE -> the manager's
stale-queue backstop fires -> `notify_idle` arms the flush -> the flush types the block
into a live turn -> agy merges it into that turn. The messages are gone: they never got
their own turn, and the flush resolved them as delivered.

That is an A1a swallow — the forbidden failure — and it is a **conservation violation**,
not a display bug.

Status of the evidence: the mechanism is confirmed from the binary's state list and from a
first tool-chain run showing the marker cycling `down -> up -> down`. A clean second repro
was cut short because the agy account hit its quota mid-run (`Individual quota reached...
resets in 160h`), so the timing capture for a full tool chain is **not yet recorded**.
Treat B as strongly evidenced, not proven.

## 2. The two proposals, judged

**"On a new user-side transcript item, flip to RUNNING immediately."**
Right instinct, adopt it — but it only fixes the *set* side (A). Failure B is a *clear*-side
bug: something must stop us believing the turn ended. It also carries transcript-poll
latency, so it is not the fastest signal available for our own sends — we already know we
sent, at zero latency.

**"Look for the spinner glyph."**
The binary has exactly one frameset, `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`. But across 150 `capture-pane` samples
spanning a full turn, **zero** frames were ever captured. The spinner is redrawn in place
and does not survive a pane capture. As specced this does not work, and it would also make
a correctness-critical invariant depend on screen-scraping a TUI. Recommend dropping it.

## 3. Proposed design — three layers, one owner each

The mistake was making one signal answer three different questions. Split them:

| window | question | owner | status |
|---|---|---|---|
| a send is mid-flight | "is another send in progress?" | `message.lock` | already built + tested |
| a turn is open | "did a turn actually start?" | **the latch (new)** | this spec |
| a turn ended | "is it really over?" | conservative clear | this spec |

The first layer already exists and matters more than I credited: mngr holds `message.lock`
for the whole send, so a second send arriving mid-flight finds it contended and is held
without ever consulting the marker (`test_an_in_flight_send_means_busy_without_reading_the_marker`).
The latch does not need to cover that window — it covers the one *after* the send returns.

### SET — on first commitment, not on submit

The latch is set when a **new `STEP_TYPE_USER_INPUT` (14) row with source `USER_EXPLICIT` (4)
appears in the transcript**, and nowhere else.

Not on POST, and not on our own submit returning. The reasons:

- **It is the A4 principle applied to turn state.** A submit that returns is an ack; a user
  row in agy's own store is a commitment. Latching on ack would set a turn-open flag for a
  turn that never started (a rejected prompt, an exhausted quota — both of which we hit live
  today), and a stuck latch means we hold messages forever against a turn that will never end.
- **One rule covers every sender.** A web send, a terminal-typed message, `mngr message` from
  cron all land the same row. Three set-sources become one, and the terminal-typed case stops
  being a special case we merely "wait out" (E11's residual shrinks).
- **It cannot lie in the dangerous direction.** A missing row means we hold a message that
  could have been typed — late, recoverable. A false row would mean typing into a live turn —
  a swallow. The chosen signal only fails the safe way.

The `USER_EXPLICIT` qualifier matters: agy also writes `USER_IMPLICIT` (3) rows, which are
not a user starting a turn. Re-setting the latch on one mid-turn would be harmless, but
gating on explicit keeps "a turn started" meaning exactly one thing.

Residual: the row must be written and then polled. That window is bounded by the poll
interval and is strictly narrower than today's, because agy writes the user row when it
accepts the message — before it starts thinking, which is the earliest the marker can move.

### CLEAR — all three, or the latch stays set

1. the `active` marker is absent, **and**
2. the transcript tail is a settled final answer (`_tail_is_final_answer` — already written,
   already knows agy's empty-`PLANNER_RESPONSE`-before-a-tool-call quirk), **and**
3. both have held across a debounce window (proposed 1.5s, one tunable constant).

Condition 2 is what closes Failure B: mid-tool-chain the tail is a tool call, not a final
answer, so a marker that flaps to `idle` between calls cannot clear the latch.

### Consequences

- `session.send` asks the latch -> a send after a marker flap is held, not typed.
- the flush gate asks the latch -> no flush into a live tool chain, so no swallow.
- the dot reads the latch -> RUNNING the moment the turn commits, and steady across tool calls.

## 4. Where it lives

One new module, `harnesses/antigravity/turn_state.py`, holding the latch beside the queue
tracker and shared through the same per-agent registry (`get_tracker`'s pattern). Touch
points: `session.send` (set on submit, read instead of the marker), `watcher` (set on new
user step; clear check in the scan loop), `activity.py` (derive from the latch),
`tap.py` (`_is_turn_open` reads the latch).

Deliberately NOT a mngr change: `statusline.sh` cannot report what agy does not tell it.

## 5. Tests

Unit (`turn_state_test.py`):
- a new `USER_INPUT`/`USER_EXPLICIT` row sets the latch; a `USER_IMPLICIT` row does not.
- the latch does NOT clear while the tail is a tool call, even with the marker absent.
- it clears once marker-absent + final-answer tail + debounce all hold.
- a marker that flaps down and back up mid-chain never clears it.
- an already-set latch is not re-armed by a later row in the same turn (no double-open).

Regression, one per reported failure:
- **A:** with the marker still absent, a send after a committed turn is held, not typed.
- **B:** drive a tool chain where the marker drops between calls; assert no flush is armed
  for the whole chain, then assert exactly one fires after the final answer.

Storm: add a "marker flaps mid-turn" op so the swallow is caught by the conservation ledger
rather than only by a targeted test. This is the one that would have caught B originally —
the current storm never drops the marker mid-turn, which is exactly why it passed while
production swallowed messages.

Live (needs quota): send during a tool chain -> stays queued; confirm one flush after the
final answer; confirm the dot holds steady across the whole chain instead of flickering.

## 6. Open questions and one recommendation

**Recommend keeping marker-up as a secondary SET source.** The spec above sets only on the
user row, per your call. I would additionally set on the marker appearing, because SET only
ever errs safe: a latch set too eagerly holds a message (late, recoverable), while a latch
set too late types into a live turn (a swallow). Setting on the marker costs nothing and
covers the narrow gap between our send returning and the user row being polled. Your call —
the spec works either way, and I will build whichever you pick.

- Debounce length is a guess (1.5s) until a clean tool-chain capture exists; it trades dot
  latency against flush safety.
- Whether agy ever durably persists a non-terminal `status` is unresolved: every row sampled
  after settle was `status=3`, so the two-phase tool emission may rely on catching a
  transient that a slow poll can miss. Worth confirming when quota returns.
- Does agy write the user row for a prompt it then rejects (quota, refusal)? If yes, the
  latch opens on a turn that never runs and only the clear-side debounce ends it. Cheap to
  check once quota is back, and it is the one case where commitment-not-ack could still bite.
