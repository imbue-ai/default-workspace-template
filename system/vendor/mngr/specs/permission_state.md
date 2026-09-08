# Permission-card state: consolidated diagnosis and rework plan

## Purpose

The in-chat permission cards (rendered by system_interface in default-workspace-template, "dwt" below) can go stale (still offering Review & respond for a request the user already answered) or inaccurate (showing Approved for a denied request and vice versa).
Two investigation attempts diagnosed different layers of this: `preston/suspicious-hertz-299e5c` (+ its paired mngr branch `preston/permission-resolution-request-id`) and `preston/blissful-keller-cc6ec0`.
This spec consolidates their diagnoses into a single issue and gives the plan for reworking the cards so their state is never stale and never inaccurate.
Read [apps/minds/docs/latchkey-permissions.md](../apps/minds/docs/latchkey-permissions.md) first for the intended end-to-end flow; this spec documents where reality diverges from it.

## The issue

A permission card's rendered state is not derived from any authoritative record.
Request/verdict state is denormalized across four stores, synchronized only by push-based, at-most-once messages that (until the attempt branches) carried no request id:

| Store | Where | Durable? | Always correct? |
|---|---|---|---|
| Gateway pending set | desktop-resident latchkey gateway | yes | no: a failed deny-time DELETE leaves an answered request "pending" |
| `RequestInbox` + response event log | minds desktop client; log at `~/.minds/events/requests/events.jsonl` | log yes, inbox no | log yes; inbox loses concurrent writes |
| Agent transcript | request tool-call/result + plain-English resolution nudge via `mngr message` | yes | no: the nudge is best-effort and can be dropped forever |
| `shellResolutions` map | system_interface iframe, fed by `minds:permission-request-resolved` | no (in-memory) | only while the iframe lives |

The card renders from the two least reliable stores: the transcript-classified resolution wins, else the shell-pushed verdict, else it shows live Approve/Deny buttons.
Every observed failure is one of: a push that never arrived (staleness), a push attributed to the wrong request, or a store update that overwrote a concurrent one (inaccuracy).
The one record that is always written correctly -- the response event log -- is read only once, at startup, to seed the inbox; nothing ever consults it to answer "what happened to request X?".

## Failure modes

**F1 -- verdicts swap between concurrent requests (inaccurate).**
The resolution nudge carried no request id, so the timeline walk in `turn-grouping.ts` attributed each verdict to the oldest still-open request (FIFO).
Deny is fire-and-forget in the popup while grant can block on a real OAuth flow, so denying a newer request while an older grant is mid-sign-in resolves them out of creation order and swaps the two cards' verdicts.
A message that batches several permission requests also shared one verdict across all its cards.
Diagnosed and fixed (unlanded) by `preston/suspicious-hertz-299e5c` (dwt `b329c845b`) + `preston/permission-resolution-request-id` (mngr `2b012744de`).

**F2 -- the verdict never reaches the transcript (stale forever).**
The nudge is the only durable input the card trusts, and its delivery is best-effort: `_send_with_retries` (latchkey/handlers/messaging.py) walks a (2, 5, 10, 20, 30, 60)s backoff, gives up permanently after ~2m07s, and abandons the backoff on app shutdown.
`load_response_events` is read once at startup purely to seed the inbox; there is no redelivery path anywhere.
Resolve a request while its workspace is stopped (or quit minds mid-backoff) and the card offers Review & respond forever, across every reload.
Not theoretical: the primary dev machine's minds.log showed 0 successful nudge deliveries ever and 2 permanent give-ups on 2026-08-24 alone.
Diagnosed and fixed (unlanded) by `preston/blissful-keller-cc6ec0` (mngr `87bc2c5f2b`, durable outbox).
(An adjacent macOS `_scproxy` delivery segfault was already obsoleted by the warm-process `MngrCaller` redesign; see the `gabriel/permission-message` reference.)

**F3 -- the live relay is ephemeral and gated (stale until F2's nudge lands, so with F2, stale forever).**
`shellResolutions` (permission-card.ts) is wiped by any iframe reload, and `notifyRequestResolved` (apps/minds/frontend/src/views/shell/shell-state.ts) deliberately relays only to the displayed workspace, on the stated assumption that a rebuilt page will find the transcript resolution already landed.
F2 falsifies that assumption, so "resolve workspace A's request while viewing workspace B" leaves A's card on live buttons.

**F4 -- a resolved request flips back to pending (stale and inaccurate).**
Six code paths across three kinds of thread updated the desktop client's `RequestInbox` slot with a bare read-modify-write.
The gateway permission-requests consumer read the inbox, repaired the requesting host's `latchkey_permissions.json`, then wrote an update built on the pre-repair snapshot, erasing any grant/deny recorded during the repair; the request re-rendered with live Approve/Deny buttons.
The gateway re-emits every still-pending request on each stream reconnect, so this window was hit routinely; a restart cleared the stale card only because the on-disk response log had been written correctly all along.
Diagnosed by `preston/blissful-keller-cc6ec0` (mngr `3db1d45c7a`); the fix was reverted (`372717d806`) to keep that branch scoped to the outbox, so the diagnosis stands unlanded.

**F5 -- an answered request is re-ingested (hygiene, not staleness).**
A deny-time DELETE to the gateway can fail, leaving the gateway's pending set holding an answered request.
A restart reloads response events but not requests, so the next stream reconnect redelivers the request and the client re-ingested it: the request log grows, host permission recovery re-runs, and the SSE wakes for nothing.
(The recorded response still suppresses the pending card, so -- correcting this spec's earlier wording -- no live buttons reappear; a red-check against main confirmed the re-ingestion but not a revived card.)
Diagnosed in the same reverted commit as F4.

## Where the attempts stand

Implemented: the branch carrying this spec (`preston/permission-state`, mngr + dwt) lands all four plan paragraphs -- the cherry-picked id correlation, the embed-contract v3 resolutions message with chrome-pushed load-time snapshot hydration, persistent in-process nudge retries, and the immediate FIFO-fallback deletion.
The deletion pass then went further than the plan: the mirrored `RequestInbox` itself is gone -- the gateway's persisted queue is read on demand (`latchkey/pending_requests.py`, one module answering "what is pending?"), the verdict index is append-only so the locked update (and the race it guarded) is deleted rather than defended, the stream consumer is a stateless first-sight change signal, host recovery moved to grant time, the four handlers share one resolve epilogue, the legacy agent-written JSONL request channel (dead since latchkey 2.9.0) is removed end to end, resolution notices carry a machine-readable `(resolution: ..., request_id: ...)` tag so the chat harness stops regexing handler prose, and the card's truncated-JSON recovery is retired in favor of the backend's structured `permission_request` field.
A second deletion pass removed the parallel `RequestEvent` display hierarchy as well: the desktop client now consumes `StreamedPermissionRequest` (the gateway's own typed shape) everywhere, dispatching handlers by the wire `request_type` and reading type-specific fields off the typed payload, which also removed the per-read fabricated request timestamps.
Verified red-on-main: each failure mode's regression scenario was also run against main's actual code (scratch harnesses, since main predates the fixes' APIs) and failed there exactly as diagnosed -- F1 rendered a granted card as "denied", F2 dropped the verdict when the workspace outlived the retry ramp, F4 erased a deny that landed mid-recovery, F5 re-ingested an answered redelivery -- while the equivalent branch tests pass; the dwt suite was additionally run with the v3 contract copied over the vendored snapshot (simulating the next vendor sync), un-skipping the contract-path test, 857/857 green.
Not verified: a live end-to-end reproduction in the running minds app, on either side of the fix.
The plan text above was revised during implementation to hold the code footprint down: the v2 push type is superseded by the one resolutions message, the workspace-side query/retry scheduler collapsed into a chrome-pushed load-time snapshot (the chrome already knows when a frame loads, so the workspace never asks, times, or retries), the durable outbox became persistent retries, and the fallback deletion moved from "later, behind a CLEANUP marker" to "now".
The rest of this section records where things stood when the spec was written.

When the spec was written, nothing from either attempt was on main in either repo, and all three attempt branches were local-only: `preston/permission-resolution-request-id` in the `mngr-notification-integration` checkout, `preston/suspicious-hertz-299e5c` in that checkout's dwt worktree, and `preston/blissful-keller-cc6ec0` in the `mngr-internal` checkout.

## Plan

Adopt one invariant and one key: the desktop client's response event log is the sole authority for verdicts, the gateway `request_id` (already echoed into the transcript and reused as the request/response event id) is the correlation key everywhere, and no component may ever attribute a verdict to a request by arrival order.
Land the already-written id-correlation pair as the first step -- mngr `2b012744de` (embed the id in every resolution nudge, at the four handlers' shared `_write_response_and_notify` call site) and dwt `b329c845b` (resolve each card strictly by its own id in `turn-grouping.ts`, keeping the FIFO guess only for transcripts recorded before ids shipped).
That alone eliminates the swapped and shared verdicts (F1).

Hydrate the card's page over ONE message, pushed by the side that already knows.
The chrome knows the exact moment it (re)loads a workspace frame, so it pushes that workspace's recent verdicts (from the response event log, via `/ui/api/inbox/resolutions`) into the fresh page as a `minds:permission-resolutions` snapshot -- the workspace never queries, times, or retries anything.
That same message, with a single unsolicited entry, replaces the v2 `minds:permission-request-resolved` push entirely -- one verdict channel, one workspace-side handler, one cache -- so the push's iframe-reload amnesia and displayed-workspace-only gating stop mattering (F3), and the old type is deleted from the contract.
The render rule then lives in one place: a card shows Approve/Deny buttons only while both the transcript and the hydrated cache say pending, and shows the verdict receipt whenever either names one; outside minds (no embedder) the card behaves exactly as today, transcript-only.

Make the write paths honest so the authority never lies and the transcript converges.
Re-land the reverted inbox commit (mngr `3db1d45c7a`) rescoped: every `RequestInbox` mutation goes through one locked update that re-evaluates the caller's decision against the current inbox, and a gateway redelivery whose id already has a response event is dropped instead of re-ingested (F4, F5).
For the nudge (F2), keep retrying in-process at the ramp's final interval until delivery or app exit, instead of giving up after two minutes -- with the card's truth now pulled from the response log, the nudge only wakes the agent sooner, so it no longer warrants a durable outbox (the `87bc2c5f2b` store was built, then dropped for a ~400-line-smaller footprint; a nudge lost to an app exit costs only the agent's early wake-up, and the agent catches up when next spoken to).
(The `_scproxy` forkserver fix this paragraph originally planned to resurrect turned out to be obsolete -- see the note under F2.)

Verify by the invariant, and retire the guesses immediately.
Each layer gets a test asserting "a request with a recorded verdict can never render live buttons": the turn-grouping out-of-order and batched-resolution tests (already on suspicious-hertz), an embed-contract test that a reloaded iframe rehydrates a shell-resolved verdict, and the locked-inbox and redelivery-drop tests (already on the reverted commit).
Delete the FIFO fallback outright rather than deferring it: hydration recovers pre-id verdicts from the response log by the same gateway id, so the arrival-order guess -- the very mechanism that swapped verdicts -- protects nothing worth its code; an unembedded view of a pre-id transcript shows honest pending instead of a possibly-wrong guess.
Update `latchkey-permissions.md` and `embed-contract.md` to describe the pull-hydrated model.

## References

- mngr `2b012744de` -- "latchkey: embed the request id in agent-facing resolution notices" (local branch `preston/permission-resolution-request-id`).
- dwt `b329c845b` -- "system_interface: correlate permission-resolution verdicts by request id" (local branch `preston/suspicious-hertz-299e5c`).
- mngr `3db1d45c7a` -- "Stop losing grant/deny responses to a racing inbox write" (reverted by `372717d806`; local branch `preston/blissful-keller-cc6ec0`).
- mngr `87bc2c5f2b` -- "Queue permission-resolution nudges so a verdict is never dropped" (same local branch); its diagnosis holds, but the outbox itself was superseded by persistent in-process retries (see the plan's third paragraph).
- mngr remote branch `gabriel/permission-message` (`e7fa1a21ee`) -- stale macOS `_scproxy` forkserver fix, never PR'd; obsoleted by the warm-process `MngrCaller` redesign (see F2) and safe to close.
- Key code: `system/apps/system_interface/frontend/src/views/permission-card.ts` and `turn-grouping.ts` (dwt); `apps/minds/imbue/minds/desktop_client/latchkey/handlers/messaging.py`, `apps/minds/frontend/src/views/shell/shell-state.ts`, `apps/minds/imbue/minds/desktop_client/static/embed_contract.js` (mngr).
- Docs to update when this lands: [apps/minds/docs/latchkey-permissions.md](../apps/minds/docs/latchkey-permissions.md), [apps/minds/docs/embed-contract.md](../apps/minds/docs/embed-contract.md).
