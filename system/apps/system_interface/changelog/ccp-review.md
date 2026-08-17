Added codex and pi as peer chat harnesses alongside claude, behind a per-harness abstraction, and brought all three onto one message-lifecycle contract.

This entry describes the end state relative to main, not the path taken to it.

## The harness abstraction

`system_interface/harnesses/` is the new home for everything a chat harness needs. `HarnessSpec` (`harnesses/registry.py`) declares one harness: its session watcher, activity tracker, model resolver and catalog, model-state path, the special-event kinds its parser may emit, and its interrupt-to-composer implementation. Claude's watcher, session parser and auth moved out of the package root into `harnesses/claude/`; codex and pi have peers beside them.

Agent creation selects the harness with `mngr create --type <harness>`, which resolves `[agent_types.<harness>]` directly, and layers only the `chat` role template on top. The per-harness create templates held nothing but `type`, so they were redundant.

The launchers are gated by `FEATURE_FLAG_ENABLE_OTHER_HARNESSES`, which is off by default. That flag gates the new-tab menu items and nothing else: `/api/harnesses` always ships every catalog, so an already-running codex or pi agent keeps its model bar with the flag off. With the flag on the three items read `New Claude agent`, `New Codex agent`, `New Pi agent`; with it off the single claude item keeps its plain `New chat` label.

## Codex, on the stock app-server

Codex runs against the unpatched `codex app-server` over one live, thread-bound JSON-RPC connection per agent. The fork is gone.

`harnesses/codex/ledger.py` is the single backend authority for a codex agent's message lifecycle, a pure reducer over the daemon's notification stream. Every accepted message sits in exactly one of Sending / Queued / Delivered / Returned. Delivery is decided by the committed `userMessage` item, never the ack, with a `thread/read` uncertainty guard for a non-full `itemsView`, and it keys on codex's own `item.id` once the message commits — the frontend-minted id survives only as a correlation token. The queue is ephemeral: no durable journal, swept empty when the thread goes idle, never revived. Messages from a foreign `clientId` (someone typing into the `--remote` terminal, or another client) commit into the transcript but never create one of our chips.

The ledger owns the live user-turn: on commit it removes the queued chip first, then emits the turn, so a message is never visible as chip and turn at once. The rollout-file reader still owns the full committed transcript and all agent output for the page-load rebuild, but no longer emits user turns to the live stream.

Stop interrupts natively — one `turn/interrupt`, then a single authoritative settle that Delivers whatever committed before the stop and Returns the rest in send order, clearing the activity dot at once. Each Returned message reaches the composer exactly once. The shoulder tap is offered only when nothing is Sending and the queue is non-empty, because codex's parked steers are already inside the running turn.

Two things codex's daemon does not give us, worked around rather than hidden: it stopped emitting `turn/started` / `turn/completed` in 0.147, so activity derives from `thread/status/changed`; and it fires no `UserPromptSubmit` hook on a programmatic `turn/start`, so the transcript watcher falls back to the newest rollout file when the `codex_transcript_path` marker is absent. The marker still wins when present.

## Pi

Pi integrates through the TypeScript lifecycle extension mngr loads, since pi exposes no shell-hook surface. Its model bar reads the model pi actually started with, with pi's per-model thinking levels as the effort options and no fast toggle. Interrupting pi returns a still-sending message to the composer alongside the queued ones, in send order. Queue chips appear and clear promptly, and a chip is removed before its message appears as a turn.

## The message-lifecycle contract

`docs/design/harness-message-lifecycle-contract.md` is the spec all three harnesses are held to: what Sending / Queued / Delivered / Returned mean, the ordering rule that a chip is removed before its turn appears, and Part E's per-harness conformance gaps — including the ones we chose not to close and the upstream limitations behind them.

Four conservation suites enforce it (`test_claude_`, `test_codex_`, `test_codex_dual_channel_message_lifecycle_conservation.py`, `conservation_storm_test.py`): seeded randomized storms of Send / Queue / Shoulder-tap / Interrupt interleavings, asserting after every step that the four states partition the total with nothing lost or duplicated.

Stop holds mngr's per-agent message lock with a bounded wait everywhere — claude's restart-drain path, claude's empty-queue chord path, and the default implementation a future harness inherits — so a stop can no longer stall behind a slow send.

## Model bar

One read path for all three harnesses: each writes `model_state.json` at its declared location, and the shared reader matches it against the harness's catalog. All three are `EAGER_THEN_RECONCILE` — the chip moves on click and reconciles when the change lands.

Claude's bar shows the agent's *effective* model, effort and fast state rather than the settings preference, so a silently-unavailable fast mode or an auto-fallback is visible. Codex mirrors every `thread/settings/updated` into its state file and switches via `thread/settings/update`; an unavailable model is a silent no-op, since the daemon falls back and echoes the effective value. Pi switches through its extension.

Each harness declares a `powered_by_label` ("Claude Code", "Codex", "Pi Coding") shown beside the bar. The agents store now skips a push byte-identical to the previous one, so a turn's transcript churn no longer redraws the bar.

## Frontend

Outgoing messages, queued-message chips and the model bar were reworked (`OutgoingMessages.ts`, `QueuedMessageView.ts`, `ModelBar.ts`, `MessageInput.ts`). Stop clears lingering "Sending…" indicators on a successful interrupt — only those that existed before it, so a message sent while the interrupt is in flight is untouched. The shoulder tap no longer errors when it races a send it cannot act on. Claude's post-auto-compaction summary collapses into a chip instead of rendering as a giant user bubble.

## Repo

Dockerfile pins codex 0.147.0, Node 22 and pi 0.83.0, plus a pi npm-cache warmer. `.mngr/settings.toml` declares the codex and pi-coding agent types. `system/scripts/pull_upstreams.sh` and `push_upstreams.sh` split work between this workspace and mngr-internal. HTTP 404/405 keep their real status codes instead of being wrapped into 500s, and the destroy subprocess cap is a named constant at 120s (a real destroy measures ~16s idle and the old 30s cap SIGTERMed destroys mid-teardown).

## De-coupling pass (post-review)

The review's harness-abstraction concerns are addressed directly rather than deferred.

**One session per agent (`harnesses/session.py`).** Every concern with a class on `HarnessSpec` already dispatched with zero branches; the codex special-casing in `agent_manager.py`/`server.py` was the one per-agent concern with no spec entry — the live daemon connection + ledger. `AgentHarnessSession` now owns the send (and its Sending records), tap availability, the native tap/interrupt dispatch, daemon liveness and the per-agent model options; `FileHarnessSession` covers claude and pi, `CodexHarnessSession` wraps the existing connection unchanged. The `SendingRegistry` moved off the watcher onto the session (the watcher is a pure transcript reader again), `shoulder_tap_class` joined `interrupt_to_composer_class` as spec data, the auth preflight and launch overrides became spec fields, and one `POST /api/agents/create-chat` with a `harness` field replaced the per-harness create routes. Zero `HarnessType.*` branches remain in `agent_manager.py` or `server.py`.

**The backend owns rendering.** Every parser runs the shared detector table (`harnesses/message_display.py`) and ships the render *decision* on the wire (`display`/`display_label`/`display_body`/`resolution` on user messages, `display` on tool calls) instead of raw harness markers — `is_meta`/`is_compact_summary` left the wire, and the frontend maps fields onto its kind catalogue with zero content sniffing. This deleted an existing duplicate: `activity_state.py` hand-mirrored the frontend's detector regexes "so the two agree." The latchkey permission machinery moved to shared `harnesses/tool_output.py` and runs in all three parsers, so codex and pi gain permission cards and stop silently losing tk step decoration past the output cap. A per-harness event-contract test ratchets the shared field set.

**One activity tracker shape.** The base owns signal caching and the universal gates (dead lifecycle → IDLE, stale tail → IDLE — the latter was a caller-side override falsely documented as redundant, and was codex's only dead-process settle); `_derive_working` is the only per-harness method, pi's tracker is a claude subclass, and the tracker-declared `active_marker_filename` (None for codex) deleted the last harness branch in the manager. The codex stop now settles the dot like every other harness, so a daemon dying mid-interrupt no longer strands it lit.
