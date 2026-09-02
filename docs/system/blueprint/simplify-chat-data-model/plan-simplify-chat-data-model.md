# Simplify the chat data model

## Overview

- Replace the claude watcher's two-tier cache (locator index + bounded body LRU + re-parse-from-disk) with the simple full-residency model the codex/pi watchers already use, generalized into one shared store all four harnesses converge on.
- Full residency is affordable because payloads leave the resident events entirely: tool inputs, tool outputs, and readable thinking blocks are loaded on demand from the source transcript, never held in backend memory and never cached backend-side.
- The two real memory leaks are fixed structurally: watchers are evicted when an agent stops or is destroyed (including externally observed death: OOM shed, idle stop), and the activity tracker folds event deltas instead of re-materializing the whole transcript per event.
- The SSE delta channel gets bounded per-connection queues with evict-and-close on first overflow; the client's existing reconnect-with-snapshot makes that safe. The per-chat WebSocket conversion stays deferred (default-workspace-template issue #521).
- pi gains cross-rotation history: all of a pi agent's session files are registered chronologically, so a backend restart no longer forgets pre-`/new` history.
- Everything lands as one PR on default-workspace-template; the paired mngr branch/PR stays as-is (auth merge + changelogs) for CI branch pairing. Memory goals are verified by a manual RSS protocol recorded in the PR.

## Expected behavior

- Reading a chat is unchanged: same paged `/events` API (offset/total/before/after), same windowed frontend, same turn grouping, progress view, subagent cards, queued chips, activity dots.
- Collapsed tool rows render from resident labels (now derived from the *full* input, so labels can no longer be clipped mid-derivation). Expanding a row fetches the full, untruncated input/output on demand, with a brief loading state; fetched payloads are cached frontend-only per agent for the panel's lifetime, and expanded state survives virtualization remounts.
- Failed tool calls stay glanceable: a small error snippet is stamped resident at parse time; the full error text loads on expand like any other output.
- Assistant messages with readable reasoning (codex at minimum; pi if its transcript records it) show a tiny muted "thinking" toggle that expands inline. Claude never shows one: its encrypted thinking blocks are useless to the user and are neither counted nor loaded.
- Payload fetches return the whole payload uncapped. When the source bytes are gone (rotated/rewritten file, post-eviction edge cases), the backend falls back to scanning the session file(s) for the event's `message_uuid`; only if that also fails does the UI show a quiet "payload no longer available" placeholder.
- Stopping or destroying a chat (from the UI, `mngr stop`, OOM shed, or idle shutdown) frees all of its chat-backend memory within one observe cycle: watcher thread, watchdog/inotify watches, and the whole parsed store. Reopening a stopped chat rebuilds from disk transparently.
- A pi chat's full history (across `/new` rotations) survives backend restarts and watcher eviction.
- A wedged SSE consumer is disconnected the moment its queue overflows; the frontend reconnects and refetches the tail, so transcripts can no longer desync or hold unbounded server memory.
- tk step titles/status and permission-request cards keep working exactly as today, now driven by structured parse-time stamps instead of truncation-surviving raw text.
- During a live workspace update there is a brief window where an already-open page's old JS receives payload-free events (expanded rows look empty); the update flow's unconditional reload closes it. No compat shims.

## Implementation plan

### Backend: shared store and watcher base (`system/apps/system_interface/imbue/system_interface/harnesses/`)

- NEW `transcript_store.py`:
  - `SessionFileState`: session_id, path, read cursor (`byte_offset_consumed`, `last_mtime`, `partial: bytes`), `emitted_count`, `events: list[dict]` in append order.
  - `TranscriptStore`: ordered `files: list[SessionFileState]` (chronological session order) + agent-wide `ref_by_event_id: dict[event_id, (SessionFileState, index)]`.
  - Query methods reusing the current claude collect logic minus body resolution: tail / before / after / at_offset / offset-of / total-count / all (concatenation), each O(limit + file count).
  - Append path: new id ⇒ append + index; known id ⇒ supersede in place, and if below `emitted_count`, queue for re-broadcast (generalizes codex's `_superseded_pending` and replaces claude's unlinked-parent rebroadcast cache).
  - Truncation/rewrite reset per file: drop that file's events from list + index, reset cursor and `emitted_count`, re-read (caller hook for claude's queue-tracker reset).
  - Internal per-event source fields (`file key, byte_offset, byte_len`), stripped before events reach the wire or SSE.
- NEW shared watcher base (same module or `harnesses/store_watcher.py`): `PathWatcher`-driven loop (start/stop/wake), per-cycle: discovery hook → incremental read per file → per-harness parse hook → store append → emit past `emitted_count` (bodies already resident). Replaces claude's hand-rolled observer/poll thread.
- `session_watcher.py`: ABC gains `get_event_detail(event_id) -> dict | None` (full input/output/thinking, read stateless from source). Queue/tap/interrupt hooks unchanged (contract-bearing; the parity feature set depends on them).
- `claude/watcher.py`: rewrite on the base. Keep: main-session discovery from `claude_session_id_history` (insert-position ordering), subagent discovery + meta.json linkage, queue-signal feeding scoped to the latest main session with dead-epoch exclusion, `get_latest_main_session_file`. Delete: `EventLocator`, body LRU, `_resolve_bodies_locked`, `_reparse_line_locked`, `_locator_ref_by_id`, `_existing_event_ids`, `_unlinked_agent_parent_events`/`_agent_parent_event_ids` (subagent enrichment mutates resident events + marks for re-broadcast instead).
- `codex/watcher.py`, `pi_coding/watcher.py`: port onto the base (mostly deletion of their bespoke `_events`/`_event_index`/cursor code). pi additionally registers *every* file in its sessions dir in chronological order (not just the marker's current file); the marker still names the live file for queue/tap anchoring.
- `antigravity/watcher.py`: adopt the store where it fits; `get_event_detail` re-queries its conversation store instead of a byte-range read.
- Per-harness `parse_detail` (in each harness's parser module): re-parse one source line/record into `{input, output, thinking}`. codex extracts reasoning items; pi if recorded; claude returns input/output only (encrypted thinking excluded by design — documented in code).

### Backend: parser and event-shape changes

- `harnesses/events.py`: document the payload-free wire contract (events carry identity, prose, labels, stamps — never raw tool input/output/thinking) and the reasoning for it; retire `MAX_TOOL_INPUT_PREVIEW_LENGTH` / `MAX_TOOL_OUTPUT_LENGTH` as wire caps (a new small constant caps the error snippet).
- `claude/session_parser.py` (and codex/pi/antigravity parsers):
  - Stop emitting `input_preview` and `output` on resident events; record the source byte range instead.
  - Derive `header_label`/`caption_label` from the full input.
  - Stamp: `has_thinking` (never for claude), `error_snippet` on `is_error` results (first line, small cap), structured tk step facts from the full command (via `tk_command_parsing`), structured tk output-decoration facts, `permission_request` (already lifted pre-truncation today).
- `harnesses/tool_output.py`: truncation-for-wire logic (`truncate_tool_output`, decoration re-appending) becomes stamp extraction; permission-request location unchanged.
- `claude/tool_labels.py` etc.: `keeps_full_tool_input` retires (labels/stamps now always see full input).

### Backend: activity, lifecycle, endpoints, SSE

- `harnesses/activity.py`: `observe(delta_events)` folds incrementally — running `pending_tool_call_ids` set (add on tool_use, discard on tool_result), plain assignment of last-event type/timestamp; codex `_observe_extra` folds its turn latch from deltas. Seeded once at watcher build from the primed store. `reset()` unchanged.
- `app_context.py`: `on_events` passes the delta batch (not `get_all_events()`) to `agent_manager.update_session_events`; add `stop_and_remove_watcher(agent_id)` (pop under `_watchers_lock`, `watcher.stop()` outside it); update the seeding call.
- `agent_manager.py`: `update_session_events` consumes deltas; a composition-time watcher-eviction callback fires wherever the lifecycle is positively dead (`_stop_activity_tracking` path — never on UNKNOWN blips), so OOM/idle stops evict too.
- `server.py`: `_destroy_agent` and `_stop_agent` call the eviction; NEW `GET /api/agents/<agent_id>/events/<event_id>/detail` — locate via store ref, read bytes, `parse_detail`; on mismatch, fall back to a `message_uuid` scan of the selected session file(s); 404-with-reason otherwise. Stateless: no backend payload caching.
- `event_queues.py`: queues created with a maxsize (~1000); on the first `queue.Full` for a connection, drain + push the `None` sentinel (stream closes; client resyncs). Delete the `_event_buffers` replay machinery and `BufferBehavior` (`events.py`); `register` no longer replays; the plugin `register_event_broadcaster` hook keeps its signature with deliver-live-only semantics.
- `main.py` / `testing.py`: wire the eviction callback and the delta-based transcript broadcaster at composition time.

### Frontend (`system/apps/system_interface/frontend/src/`)

- `models/Response.ts`: event types drop `output`/`input_preview`; add `has_thinking`, `error_snippet`, tk stamps; per-agent payload cache (`Map<event_id, detail>`) living for the panel's lifetime; `fetchEventDetail(agentId, eventId)`.
- Tool-row rendering (`views/ChatPanel.ts`, `views/SubagentView.ts`, shared components): fetch-on-expand with loading and unavailable states; expanded state keyed by event id so it persists across virtualization remounts; failed rows render the resident `error_snippet`.
- Assistant message rendering: tiny muted "thinking" toggle when `has_thinking`, expanding inline via the same detail fetch.
- Progress view / permission cards: switch to the structured stamps (removing any remaining raw-text parsing of inputs/outputs).

### Docs

- Rewrite `docs/system/blueprint/scaling-design/plan-scaling-design.md` into a short doc describing the new architecture (full-residency store, payload deferral, eviction lifecycle) and why the wire stays payload-free.
- Update `harnesses/core-contracts/tool-call-policies.md` (and the messages-lifecycle contract where it mentions truncation/preview behavior). Message-conservation contracts themselves are unchanged.

### Out of scope (explicitly)

- Queue-conservation, shoulder-tap, and interrupt machinery: untouched; their watcher hooks stay stable.
- Per-chat WebSocket conversion and the chat/system-interface split: deferred to DWT issue #521.
- Idle-based eviction for running agents; user_message content deferral; payload-residency ratchet: all deliberately not done (documented reasoning in code/docs instead).
- Common-transcript convergence: deferred indefinitely.
- mngr repo changes: none.

## Implementation phases

1. **Shared store, behavior-neutral**: add `TranscriptStore` + watcher base; port codex and pi onto it (including the pi all-files history fix). Events still carry payloads at this point; all existing tests pass.
2. **Claude convergence**: port the claude watcher onto the store; delete the two-tier machinery; rewrite the conservation suites (`test_claude_message_lifecycle_conservation.py`, codex equivalents, `conservation_storm_test.py`, `claude/watcher_test.py`) freely against the store's public surface, preserving the no-message-lost contracts.
3. **Incremental activity**: delta-based `observe` + `on_events`; equivalence-tested against the old full-list derivation.
4. **Lifecycle eviction**: `stop_and_remove_watcher`, wired to destroy/stop endpoints and the positively-dead observe path; rebuild-on-demand verified.
5. **Payload deferral**: parser stamps + payload-free wire shape, detail endpoint with fallback scan, antigravity store re-query; frontend fetch-on-expand, payload cache, error snippets, thinking toggle.
6. **SSE bounding + buffer deletion**: bounded queues, evict-and-close, `BufferBehavior` removal.
7. **Docs + verification**: scaling doc rewrite, core-contracts updates, ratchet trims, full test suite, manual RSS protocol run recorded in the PR.

Each phase leaves a working system; phases 1-4 are invisible to the frontend.

## Testing strategy

- **Store unit tests**: paging methods against a brute-force oracle over multi-file, multi-event-line synthetic transcripts; supersede-in-place and re-broadcast; truncation reset; id-index dedup; source-range bookkeeping.
- **Watcher tests per harness**: incremental tail + partial lines (existing coverage, ported); pi multi-file chronological registration; claude subagent enrichment via supersede; emission exactly-once under concurrent HTTP reads.
- **Detail endpoint tests**: happy path (full payload round-trip), evicted-then-rebuilt watcher, byte-range mismatch → `message_uuid` fallback, genuinely-gone → clean unavailable; claude returns no thinking, codex does.
- **Parser stamp tests**: labels from full (previously-clipped) inputs, error snippets, tk step/decoration stamps against real fixture lines, permission requests; wire events assert payload-free shape.
- **Activity tests**: delta-fold equivalence with the previous full-list signals across turn/tool/interrupt sequences; seeding on build.
- **Eviction tests**: destroy/stop/lifecycle-dead evicts (thread joined, store released, watcher registry empty); reopening rebuilds and serves identical history; no eviction on UNKNOWN lifecycle.
- **SSE tests**: overflow disconnects exactly that consumer with the sentinel; healthy consumers unaffected; no replay on register.
- **Frontend (vitest)**: payload cache hit/miss, expand-state persistence across remount, thinking toggle gating on `has_thinking`, error snippet rendering, unavailable placeholder.
- **Conservation suites**: rewritten per phase 2; the storm test drives the store surface.
- **Manual verification** (per repo policy, not crystallized): drive the real UI — expand large outputs, thinking toggle, scroll/backfill on a long chat — via tmux/playwright; run the RSS protocol (long chat with scroll/expand, several chats opened then stopped, one destroyed; RSS + watcher-thread/inotify counts sampled at each stage, before vs after) and record results in the PR.
- Full suite + coverage per repo instructions before finishing; CI (including the paired snapshot e2e) validates the rest.

## Open questions

- pi chronological ordering source for late-found session files: filename timestamp vs mtime (pick whichever pi's naming makes reliable; mtime fallback).
- Exact error-snippet shape: first line vs first N chars (proposed: first line, capped ~200 chars).
- Whether `get_all_events` stays on the ABC or narrows to the subagent/interrupt callers once the store makes it trivial (cosmetic).
- Where the detail fetch surfaces byte size hints (e.g. "3.2 MB" on a collapsed row) — nice-to-have, not planned; would need a resident size stamp.
- Module naming (`transcript_store.py` vs folding the base class into `session_watcher.py`) — implementer's call within conventions.
