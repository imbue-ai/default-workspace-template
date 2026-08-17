# Pi harness: message-lifecycle-contract limitations

The pi harness implements the message-lifecycle contract
(`docs/design/harness-message-lifecycle-contract.md`) to parity with the claude harness.
This file records the handful of honest gaps that remain, why they are acceptable, and how
often they can actually occur. None of the first three lose or corrupt a message -- they are
brief visual transients identical to what the claude harness exhibits. The fourth is a genuine
but rare loss, deferred deliberately.

If you close one of these, delete its entry.

## 1. Queued -> Delivered can briefly double-show (contract A3b)

**What:** when a queued message drains into a committed turn, the queued chip should disappear
*before* the transcript turn appears. The backend emits the chip-removal first (see
`watcher.py: _emit_unsent`), but the two travel to the browser on **different transports** --
the queued-message snapshot on the agents WebSocket, the transcript turn on the per-agent SSE
event stream. So under transport jitter the turn can paint for a redraw or two while the chip is
still up (the message shown as a chip AND a turn at once).

**Frequency:** rare. It requires the SSE frame to overtake the WebSocket frame despite being
emitted second, and only when you are actually using the queue (typing while pi is mid-turn).
When it happens it is a sub-second flicker that self-heals on the next redraw.

**Why accepted:** the only fully-correct fix is to co-emit the chip-removal on the *same* channel
as the transcript turn (one ordered stream) or to reconcile chip-vs-turn by a shared id on the
frontend. The id path was explicitly rejected (we do not mint correlation ids; see the contract
doc's Part C notes and the harness discussion). The claude harness has the identical two-transport
structure and the identical residual, so this is at parity, not a regression.

## 2. A "Sending..." bubble can clear a beat early (contract A1a / A2 positional correlation)

**What:** the optimistic "Sending..." bubble is removed positionally (oldest-first) when any real
user turn or queued chip arrives (`frontend/src/models/OutgoingMessages.ts: noteBackendArrivals`),
not by matching the specific message. So if a user turn commits from **another browser** or from
the **agent's TUI** while your own send is still in flight, your oldest bubble can be dropped
before its own real representation appears -- briefly showing nothing for that message until its
true state arrives.

**Frequency:** rare, and impossible for a single user in a single tab: it needs a *second*
concurrent writer (another browser on the same agent, or the TUI) committing a turn inside the
sub-second window your send is in flight. Brief and self-correcting.

**Why accepted:** the contract explicitly blesses positional oldest-first correlation with
arrival-id dedup ("over-eager removal is harmless -- the real bubble is what shows"). Making it
exact would require threading a minted message id through `pi_inbox` and pi's session record --
the same rejected id-minting as limitation 1. The claude harness correlates positionally too.

## 3. "Sending..." and "Thinking..." can briefly co-show on an idle-start send (contract A6)

**What:** when you message an idle pi agent, the turn's activity ("Thinking...") and the removal
of the "Sending..." bubble ride different transports, so for a redraw or two both can be on
screen.

**Frequency:** rare, cosmetic, sub-second, idle-start only. Pre-existing (not introduced by the
lifecycle work) and present for the other harnesses.

**Why accepted:** cosmetic transient; no message is affected. Fixing it is the same
single-channel / id-reconciliation work as limitation 1.

## 4. Interrupt during a shoulder-tap flush can lose the flushed messages (contract Part D) -- DEFERRED

**What:** this is the one genuine message *loss*, and the only limitation here that is not merely
a visual transient. During a shoulder-tap flush, the mngr pi lifecycle extension
(`system/vendor/mngr/libs/mngr_pi_coding/.../mngr_pi_lifecycle.ts`) aborts pi's live turn and
holds the captured steer messages in an in-memory `pendingResubmit` while it waits for idle to
re-inject them -- and the dwt queue mirror has already been cleared by the flush sentinel. If a
Stop (retract) lands in that window, the backend reads an empty mirror (returns nothing to the
composer) and the extension then discards the resubmitted steers -- so those messages are lost:
neither Delivered nor Returned.

**Frequency:** rare. You must hit Stop within the fraction of a second between tapping Shoulder-tap
and the extension re-injecting the flushed messages.

**Why deferred:** unlike 1-3, the fix is not in this app -- it is in the mngr extension (make a
retract during a pending resubmit reclaim and return those steers rather than discard them), which
is a `libs/mngr` change with its own changelog and test discipline. It also cannot be caught by the
in-process storm test (`harnesses/conservation_storm_test.py`), whose `_PiWorld` models the
extension as committing flushed steers instantly and so has no `pendingResubmit` window; proving it
needs a test against the real extension. This is why the claude harness does not have the loss: its
flush is a dwt-owned SIGKILL-restart-drain, so the captured block never leaves the backend's hands.
Fixing pi means either fixing the extension (keep the gentle native flush) or making pi's flush
dwt-owned like claude's (lose the gentle no-restart tap). Decide deliberately when it is picked up.
