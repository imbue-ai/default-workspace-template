# Pi harness: transcript, activity, captions, and queuing — build plan

Status: plan, verified against live pi + the claude/codex harnesses. Replaces the
placeholder wiring for the `pi-coding` harness so the pi chat tab renders its
conversation, drives the activity strip, and surfaces queued messages.

Scope: transcript tailing, tool-call rendering, activity indicator + caption, and
message queuing. **Out of scope (per instruction): model/effort switching** — the
model bar already works off `pi_model_state.json` via `PiModelResolver`.

## 0. The one principle

Everything downstream of a harness's *parser* is harness-blind and already built:
the frontend transcript store, the tool-call collapsible, the activity strip, the
queued-message surface, the `/events` REST + SSE transport, and `app_context`'s
watcher wiring all consume a fixed event schema without knowing which harness
produced it. So the work is: **tail pi's own file, parse it into that schema, and
feed the (already-generic) queue surface.** No frontend changes.

Pi is modeled on **codex**, not claude: like codex, we tail the tool's *own*
native transcript, followed via a marker, not the mngr common/raw transcript. (We
do NOT read `logs/pi-coding_transcript/events.jsonl` — that is the extension's
mirror, the analogue of codex's `stream_transcript.sh` mirror the codex watcher
deliberately ignores.)

## 1. Pi's native session file (verified live)

Path: `<agent_state_dir>/plugin/pi_coding/sessions/<encoded-cwd>/<ts>_<uuid>.jsonl`.
The live file is named by the `pi_session_file` marker in the agent state dir
(written by the lifecycle extension on `session_start`/`session_switch`, and read
by the plugin to resume with `pi --session <file>`). This is pi's analogue of
codex's `codex_transcript_path` marker.

Verified properties:
- **Single file, append-only, written incrementally.** Live poll during a turn:
  9 -> 13 -> 15 -> 17 lines as tools ran. So a byte-cursor tail streams live.
- **Rotation only on `/new`** (a new session file; the marker repoints). Resume
  (`--session`) reuses and appends to the same file.
- Records are a parent-linked chain: every line is
  `{type, id, parentId, timestamp, ...}`. `id` is pi's own stable short id.

Record types (only four seen; others ignored):

| type | fields | maps to |
|---|---|---|
| `session` | `version, id (uuid), timestamp, cwd` | (skip; session id source if needed) |
| `model_change` | `provider, modelId` | (skip; model bar owns this) |
| `thinking_level_change` | `thinkingLevel` | (skip) |
| `message` | `message: <AgentMessage>` | user / assistant / tool_result event |

`AgentMessage` shapes (verified):
- **user**: `{role:"user", content:[{type:"text",text}], timestamp}`
- **assistant**: `{role:"assistant", content:[ {type:"thinking",...} | {type:"toolCall", id:"toolu_…", name, arguments} | {type:"text",text} ], model, provider, usage, stopReason, timestamp}`
  - An assistant message carries **1..N** `toolCall` blocks (parallel calls share
    one message; sequential calls each get their own message — both verified).
- **toolResult**: `{role:"toolResult", toolCallId:"toolu_…", toolName, content:[{type:text}] | string, isError, timestamp}` — one record per call.

Correlation: assistant `toolCall.id` == toolResult `toolCallId` (`toolu_…`).

Tool names are clean and lowercase: `bash, read, edit, write, grep, find, ls`
(args e.g. `bash {command}`, `read {path,limit}`). No codex-style code-mode
indirection.

## 2. The target event schema (from `Response.ts`, verified)

Three core event types; the parser emits these dicts:

**user_message** — `{timestamp, type:"user_message", event_id, source, role:"user", content, message_uuid}`

**assistant_message** — `{timestamp, type:"assistant_message", event_id, source, role:"assistant", model, text, tool_calls:[…], stop_reason, usage|null, message_uuid, is_auth_error:false}`
(mirror codex's `_assistant_event`; `is_api_error`/etc. are frontend-optional and
omitted for v1.)

**tool_call** (element of `tool_calls`) — `{tool_call_id, tool_name, input_preview, header_label, caption_label}`

**tool_result** — `{timestamp, type:"tool_result", event_id, source, tool_call_id, tool_name, output, is_error, message_uuid}`

Shared caps (`harnesses/events.py`): `input_preview` truncated to 200,
`output` to 2000.

### Parser mapping (`pi_coding/session_parser.py`)

- `source = "pi-coding/common_transcript"` (label only; nothing branches on it).
- **event_id = `pi-<record.id>`** — pi's own stable id, so a re-serialised/resumed
  record dedups against what we already emitted (same discipline as codex keying on
  its msg id / call_id). tool_result uses `pi-<record.id>` too.
- **user** record -> one `user_message`, `content` = joined text blocks.
- **assistant** record -> one `assistant_message`:
  - `text` = joined `text` blocks. **Thinking blocks are dropped entirely (never
    rendered).**
  - `tool_calls` = one entry per `toolCall` block: `tool_call_id = block.id`,
    `tool_name = block.name`, `input_preview = truncate(json(arguments), 200)`,
    plus labels from §3.
  - `model` = `message.model` or `""`; `usage` mapped from pi's
    `{input,output,cacheRead,cacheWrite}` (nice-to-have) or null.
  - Bundling text + all tool calls in one event matches claude's renderer (which
    renders `text` then each `tool_calls` block) — cleaner than codex's
    one-event-per-call, and 1:1 with pi's record.
- **toolResult** record -> one `tool_result`: `tool_call_id = message.toolCallId`,
  `tool_name = message.toolName`, `output = truncate(text(content), 2000)`,
  `is_error = message.isError === true`.
- `session` / `model_change` / `thinking_level_change` -> `[]`.

## 3. Tool-call titles + collapsibles

Rendering is `renderToolCallBlock` (`message-renderers.ts`), fully generic — one
collapsible block per tool call, reading three fields:
- **title** = `tool_call.header_label` (chevron row; click toggles
  `.tool-call-block--expanded`).
- **body** = `tool_call.input_preview` (the args) + the matched
  `tool_result.output`, error-styled when `is_error`. Result matched by
  `tool_call_id`.

So the collapsible "just works" once the parser emits the right fields. Labels
come from a new `pi_coding/tool_labels.py`, a near-copy of `claude/tool_labels.py`
using the shared helpers (`basename`, `shorten`, `quoted`, `first_string_value`,
`parse_input_preview`, `mcp_caption`). Pi verb table:

| tool | args key | header_label | caption_label |
|---|---|---|---|
| `read` | `path` | `Tool: Read` | `Reading <basename(path)>` |
| `write` | `path` | `Tool: Write` | `Writing <basename>` |
| `edit` | `path` | `Tool: Edit` | `Editing <basename>` |
| `bash` | `command` | `Tool: Bash` | `Running <shorten(command)>` |
| `grep` | `pattern` | `Tool: Grep` | `Searching "<pattern>"` |
| `find` | `path`/`pattern` | `Tool: Find` | `Searching …` |
| `ls` | `path` | `Tool: List` | `Listing <basename(path)>` |
| MCP `mcp__…` | — | `Tool: <name>` | via `mcp_caption` |
| unknown | — | `Tool: <name>` | `Running tool…` |

Header style: `Tool: <TitleCased>` to match claude's look (pi's names are
lowercase; title-case only the header). Note: unlike claude, pi's `bash` call has
no `description` field — caption the `command` directly.

## 4. Activity indicator + caption

Two halves: backend picks the *state*, frontend picks the *words* — both already
built.

**Backend state** — reuse `claude/activity_state.derive` via the existing
`PiActivityTracker` (already registered; declares
`marker_filename = "pi_process_started"`). Pi has no turn markers in its native
file (like claude, unlike codex), so the claude derivation is exactly right:

```
not running (mngr lifecycle)                    -> IDLE
transcript tail older than process-start marker -> IDLE   (restart guard)
unmatched tool_use (call w/ no result)          -> TOOL_RUNNING
last event is user_message / tool_result        -> THINKING
else                                            -> IDLE
```

- `is_agent_running` = `agent_state.state in {RUNNING, RUNNING_UNKNOWN_AGENT_TYPE}`
  (`agent_manager._recompute_activity_state`). For pi this is driven by the
  `active` marker the lifecycle extension writes on `agent_start` and removes on
  `agent_end` — **verified live** (RUNNING during the turn, idle after).
- `has_unmatched_tool_use` / tail come from the parsed events (§2), recomputed on
  every `on_events` over `watcher.get_all_events()`.

**Frontend words** (`ActivityIndicator.ts`, zero changes): `IDLE` -> hidden;
`THINKING` -> "Thinking…"; `TOOL_RUNNING` -> the pending tool call's
`caption_label` (e.g. "Reading README.md"), with a 700 ms min-hold so a fast tool
doesn't flash. The caption is literally our `caption_label`.

### Required plugin fix: write `pi_process_started`

**Verified: `mngr_pi_coding` does NOT touch `pi_process_started`.** claude writes
it from a SessionStart hook; codex `touch`es it in `assemble_command`. Without it
the staleness guard's `process_started_at` is always None, so a pi killed
mid-turn (leaving a stale `active` marker) could pin "Thinking…/Running…" forever
after restart.

Fix: mirror codex in `PiCodingAgent.assemble_command` — prefix the launch with a
marker reset + stamp:
```
rm -f "$MNGR_AGENT_STATE_DIR/active" 2>/dev/null || true; \
touch "$MNGR_AGENT_STATE_DIR/pi_process_started" 2>/dev/null || true; \
<existing resume prelude + pi invocation>
```
(Clearing a stale `active` on launch matches codex's `reset_marker_cmd`; the
extension re-creates it on the next `agent_start`.) This is a `libs/mngr` change
(vendored) and needs a changelog entry there.

## 5. Queuing — like claude's, fed from `pi_inbox`

### What `pi_inbox` is
`<agent_state_dir>/pi_inbox` is mngr's own outbound message log: `send_message`
appends one JSON-encoded string per line; the lifecycle extension polls it and
injects each new line into pi via `sendUserMessage(…, {deliverAs:"followUp"})`.
followUp parks the message inside pi if a turn is running, else starts a turn.
Verified live — the file is exactly `"message one"\n"message two"\n…`.

### Why this is simpler than both existing harnesses
The queued-message engine is the shared `QueuedSet` (FIFO `add` / `resolve_oldest`
/ `clear` / `snapshot` / `concatenated_block`). A per-harness *tracker* feeds it:
- **claude** reconstructs enqueue/leave from an opaque in-transcript ledger
  (a conservation law) — the complex part of claude's queue.
- **codex** needed a *patched binary* writing a `queued_input.jsonl` sidecar.
- **pi** gets the sidecar for free: `pi_inbox` is an explicit, ordered,
  content-carrying enqueue ledger mngr already owns. No binary patch, no
  extension change.

### Design (`pi_coding/queue_tracker.py`, ~mirrors `ClaudeQueueTracker`)
Same `QueuedSet`, same watcher surface, same UI behavior — only the source of the
enqueue/leave signals differs:
- **enqueue** = a new line in `pi_inbox` -> `queued_set.add(id, content, ts, is_phantom)`.
  - `id` = stable hash of `(session_id, line_index, content)`.
  - `is_phantom` = content starts with `<task-notification>` or is blank (reuse
    claude's `_is_phantom_content`) — so background notifications never surface.
- **leave** = each new `user_message` event draining into the native transcript
  -> `queued_set.resolve_oldest()` (positional FIFO, like claude).
- **backstop** = working->IDLE -> `notify_idle()` clears stragglers
  (interrupt/crash left no leave record).

Positional and self-correcting: feed the whole inbox, net each enqueue against a
drained user turn; only genuinely-parked messages remain. Brief flicker when a
message sent to an idle agent enqueues then immediately drains — acceptable, same
optimistic model claude/codex accept.

**Verified live.** With a 12 s turn running, two messages sent mid-turn appeared
in `pi_inbox` immediately (enqueue) but did NOT appear in the native session file
until the turn ended (drain: native user-record count went 3 -> 4 only at t=14s,
after the turn). So enqueue (inbox) and leave (native user_message) are genuinely
separated in time — the parked window we surface as "queued" is real, and the
FIFO net is correct.

### Watcher wiring (already generic — no server/app_context changes)
`app_context` calls `watcher.set_queue_snapshot_callback(...)` (bridges snapshot ->
`agent_manager.update_queued_messages` -> WS) and
`register_queue_idle_handler(id, watcher.notify_idle)`. `server.py` exposes
`/flush-queue` (`get_queued_block` then `clear_queue`) and `/drain-to-composer`.
The pi watcher just **overrides** the queue methods (like claude) and fires the
snapshot callback whenever the inbox/transcript changes.

The pi watcher therefore tails **two files** each cycle: the native session JSONL
(events + activity) and `pi_inbox` (queue). Both under the agent state dir,
watched with one `PathWatcher` on the state dir (or two targets).

## 6. The watcher (`pi_coding/watcher.py`, off codex's tailer)

Replace `PiPlaceholderSessionWatcher` with a real `PiSessionWatcher`, adapting
`CodexSessionWatcher` almost directly:
- Resolve the live file from the `pi_session_file` marker each cycle; on change
  (a `/new`), switch file from its start, keep `_events`/`_event_index`/dedup and
  the global line counter (so ids stay unique and a resumed re-serialisation
  dedups).
- Byte-cursor incremental read with a carried trailing-partial (bytes, for UTF-8
  safety). Shrink -> re-read from 0 (id dedup drops re-emits).
- `_ingest_event` with stable-id dedup + in-place supersession + re-broadcast of
  already-emitted supersessions (verbatim from codex).
- `PathWatcher` on the agent state dir (recursive) so appends to the session file
  AND `pi_inbox` wake the loop; 1 s poll safety net.
- Full read/pagination API (`get_all_events` / `get_tail_events` /
  `get_backfill_events` / `get_forward_events` / `get_events_at_offset` /
  `get_event_offset` / `get_total_event_count`) — copy codex; `session_id` inert.
- `get_subagent_metadata` -> None, `is_main_session_event` -> True (pi has no
  in-process subagents; the lifecycle extension notes only the mngr-launched pi
  runs, and it has no Task tool).
- Queue overrides delegate to `PiQueueTracker` (§5).

### Deferred (matches "don't worry for now")
- **Interrupt handling**: pi's native file has no turn-abort record, so an
  interrupted tool call has no synthetic result — its card spins until the
  process restart clears it (guarded by the staleness marker from §4). No
  codex-style synthetic "Interrupted." result in v1.
- **Direct-TUI sends** (a human typing into `mngr connect`, bypassing the inbox):
  would drain a queued slot it didn't fill. Accepted first-cut imprecision
  (claude has the same class of issue).

## 7. Build surface

New (`harnesses/pi_coding/`):
- `session_parser.py`, `tool_labels.py`, real `watcher.py`, `queue_tracker.py`
  (+ `_test.py` peers, using captured fixtures — see below).

Edit:
- `harnesses/registry.py`: `PiPlaceholderSessionWatcher` -> `PiSessionWatcher`.
- `libs/mngr` `mngr_pi_coding/plugin.py`: touch `pi_process_started` + clear stale
  `active` in `assemble_command` (+ changelog entry in `libs/mngr`).
- **`frontend/src/views/turn-grouping.ts`: teach the tk-step detector pi's `bash`
  tool name.** `isTkLifecycleCall` (line ~178) and `tkCommand` (line ~190) gate on
  `tool_name === "Bash" || "exec"` — pi's shell tool is `bash`, so without this the
  chat **step-progress timeline does not render for pi**. Two-line change,
  mirroring how codex's `exec` was added. (The stdout-marker parsing and
  `TK_LIFECYCLE_RE` — which matches the `"command"` key pi's bash uses — are
  already harness-agnostic.)

Parser must carry the tk exemptions (mirror codex's `keeps_full_tool_input`): do
NOT truncate a `bash` tool call whose command is a `tk` lifecycle op, nor its tk
output — otherwise the title/summary fallback can't parse.

Untouched: activity strip, tool blocks, queue surface, model bar, server,
app_context, transcript store.

## 7b. Parity gaps that are NOT obstacles (pi limitations / deferred)

These differ from claude but are either impossible in pi or deliberately deferred;
none blocks the core experience:
- **No subagents.** pi has no Task/Agent tool (single loop; the extension notes
  this), so no subagent cards ever — nothing to render, `get_subagent_metadata`
  stays None.
- **No permission cards.** pi runs every tool unattended (no approval gate), so
  the permission-request card path never fires.
- **Interrupt.** pi's native file has no turn-abort record, so an interrupted tool
  call gets no synthetic "Interrupted." result (deferred per instruction); the
  card spins until the process-start staleness guard (§4) clears it on restart.
- **API/auth error styling.** Omitted v1 — a pi provider error renders as plain
  assistant text (no `is_api_error`/`is_auth_error` detection). Addable later via a
  pi `error_patterns` peer.

Changelog: one entry under `system/apps/system_interface`'s project and one under
`libs/mngr` (the plugin change).

## 8. Testing

- **Unit** (`*_test.py`): parser against captured fixtures (a simple session and a
  tool-heavy session — already captured live), tool_labels per tool, queue_tracker
  enqueue/leave/phantom/idle, watcher tail+rotation+dedup (mirror
  `codex/watcher`'s tests). Save fixtures under `pi_coding/testdata/`.
- **Live manual**: message the pi agent, watch the chat tab render user/assistant/
  tool blocks, the activity strip flip THINKING/TOOL_RUNNING with the right
  caption, and a mid-turn second message appear in the shoulder-tap queue then
  reconcile. (pi is authenticated on this box; `utopian-puzzling-hamster` is a
  working pi agent.)

## 9. Open decisions (defaults chosen; override if wanted)

1. Bundle text + all tool calls into one `assistant_message` per pi record
   (claude-style). **Default: yes.**
2. Header style `Tool: Read` (title-cased) to match claude. **Default: yes.**
Both are cosmetic and reversible.
