# Antigravity (agy) transcript harness — implementation spec

Scope: everything in `imbue/system_interface/harnesses/antigravity/` needed to turn
the placeholder into a real transcript harness — message rendering, tool-call
detection + titles, the activity indicator, and the activity caption. Modeled on
the Claude harness. **Queuing is explicitly out of scope** (see §9).

Status quo: the model bar is fully wired (`model.py`, read-only catalog + resolver).
`watcher.py` and `activity.py` are placeholders. Registry entry exists
(`registry.py`, `special_kinds=frozenset()`). This spec fills in the watcher,
parser, tool labels, and a correct activity derivation.

---

## 0. The decisive fact that shapes everything

agy does **not** store its transcript as a tailable JSONL like Claude/Codex. Since
agy 1.0.4 each conversation is a **protobuf-encoded SQLite DB** at
`…/antigravity-cli/conversations/<conv_id>.db`, table `steps`
(`idx`, `step_type` int enum, `status` int enum, `step_payload` protobuf BLOB).
Verified live by probing the DB directly.

**We do not re-decode that protobuf in system_interface.** `mngr_antigravity`
already ships `resources/decode_agy_transcript.py` — a defensive, dependency-free
protobuf wire-walk that:
- reads new `steps` rows incrementally (per-conversation offset in
  `<agent_state_dir>/plugin/antigravity/.transcript_offsets/<conv_id>`),
- stops at the first non-terminal (still-generating) step so emission is in-order
  and never partial,
- decodes each into a **clean JSON record** and appends it to
  `<agent_state_dir>/logs/antigravity_transcript/events.jsonl`,
- is pinned against the live agy binary by a release-marked descriptor-diff test,
  so agy's ~weekly schema drift is caught in mngr, not here.

**Tail source (recommendation): `<agent_state_dir>/logs/antigravity_transcript/events.jsonl`.**
This is a Claude-style byte-offset JSONL tail — the exact shape the existing
watchers already know how to do — over a stream someone else keeps correct.
Rationale, and why not the alternatives, in §1.

---

## 1. Architecture decision — what the watcher tails

Three candidate sources, and the call:

| Source | Pros | Cons |
|---|---|---|
| **A. mngr's `logs/antigravity_transcript/events.jsonl`** (recommend) | clean JSONL; already incremental + offset-tracked; protobuf decode + schema-drift guard owned by mngr; nothing agy-opaque enters system_interface | lags agy by the streamer's ~1s poll; **lossy** — drops tool *result* steps and agy's own `f30/f31` captions |
| B. agy's SQLite `.db` directly (port `decode_agy_transcript.py`) | full fidelity incl. native captions + tool results; lowest latency | duplicates fragile black-magic protobuf decode in a second package; two places to chase agy schema drift |
| C. agy's `brain/<conv>/.system_generated/logs/transcript.jsonl` | clean JSONL, richer (inline tool-result text) | per-conversation; a legacy path agy may stop writing; not offset-managed for us |

**Pick A.** Codex deliberately tails its *own* live rollout instead of mngr's mirror
because the mirror lagged up to 1s — but that reasoning doesn't transfer: **agy
only writes a step row when it completes** (confirmed live — the DB row count stayed
flat while a tool ran and jumped only on completion), so agy's own store is already
completion-granular. The streamer's extra ~1s is immaterial for rendering completed
messages, and the busy/idle indicator does **not** come from the transcript anyway
(it comes from the mngr lifecycle + process marker, §5). Reserve B only if we later
want native captions or tool-result bodies.

Consequence to design around up front: **the stream contains no tool-result events.**
That breaks the reused Claude activity heuristic and shapes §4 and §5.

### The record schema we consume (from `decode_agy_transcript.py`)

One JSON object per settled step:

```
step_index   int        # order within the conversation
source       str        # USER_EXPLICIT | USER_IMPLICIT | MODEL | SYSTEM | SYSTEM_SDK | STEP_SOURCE_<n>
type         str        # USER_INPUT | PLANNER_RESPONSE | ERROR_MESSAGE | CONVERSATION_HISTORY | SYSTEM_MESSAGE | STEP_TYPE_<n>
status       str        # PENDING|RUNNING|DONE|INVALID|CLEARED|CANCELED|ERROR|GENERATING|WAITING|QUEUED|INTERRUPTED
created_at   str        # "YYYY-MM-DDTHH:MM:SSZ" (may be "")
content      str        # present on USER_INPUT / PLANNER_RESPONSE / ERROR_MESSAGE
thinking     str        # optional; on PLANNER_RESPONSE only
tool_calls   [ {name: str, args: str} ]   # optional; on PLANNER_RESPONSE only. args is a JSON string.
_mngr_conv_id str       # the conversation UUID
```

Only `USER_INPUT`, `PLANNER_RESPONSE`, `ERROR_MESSAGE` carry renderable content; the
decoder drops everything else (tool-result/system/history steps). Multiple
conversations for one agent are interleaved in the same `events.jsonl`, tagged by
`_mngr_conv_id` (matters for resume; see §3 ordering).

---

## 2. Files to create / change in `harnesses/antigravity/`

```
antigravity/
  watcher.py         REPLACE placeholder → real JSONL-tailing watcher
  session_parser.py  NEW  record → shared UI events
  tool_labels.py     NEW  (name, args) → header_label / caption_label
  activity.py        REWRITE derivation (placeholder heuristic is wrong for agy)
  activity_state.py  NEW  pure agy activity derivation
  model.py           KEEP (done)
  icon.svg           KEEP
  __init__.py        KEEP empty
  watcher_test.py    NEW
  session_parser_test.py NEW
  tool_labels_test.py    NEW
  activity_state_test.py NEW
```

Registry (`harnesses/registry.py`): entry already present; keep
`special_kinds=frozenset()` (agy stream has no turn markers). No spine changes.

---

## 3. `watcher.py` — real `AgentSessionWatcher`

Mirror the Claude/Codex watcher shape but simpler: single JSONL, append-only, no
subagents, no rotation-rewrite (mngr only appends).

### Source path
```
transcript = agent_info.agent_state_dir / "logs" / "antigravity_transcript" / "events.jsonl"
```
`AgentInfo.agent_state_dir` is available (used by Codex the same way). No marker-file
indirection needed — the path is stable (unlike Codex's rotating rollout).

### Tailing (reuse Claude's proven mechanics, minus rotation)
- `SessionFileState`: `byte_offset_consumed`, `last_mtime`, `locators` (append-only
  `EventLocator` list), `emitted_count`. Same two-tier memory model as Claude
  (bodyless locators + bounded LRU body cache) — or, given agy transcripts are
  small, a simpler "hold parsed events in a list" is acceptable for v1; keep the
  locator interface so paging methods are cheap.
- Watchdog on the `logs/antigravity_transcript/` dir + 1s poll safety net
  (`watcher_common.POLL_INTERVAL_SECONDS`, `WakeOnChangeHandler`) — identical to
  Claude.
- Incremental read: `seek(byte_offset_consumed)`, read tail, split at last complete
  line (`_split_at_last_complete_line`), parse complete lines, advance offset. mngr
  writes whole lines (`sink.write(line + "\n")`), so partial-line handling is only a
  mid-write safety net — reuse Claude's helper as-is.
- **No truncation/rotation branch**: mngr's `events.jsonl` is append-only across the
  agent's life. If `size < byte_offset_consumed` ever occurs (unexpected), fall back
  to Claude's purge-and-reparse to stay safe, but it should never fire.
- Emission: `_poll_for_changes` broadcasts every locator past `emitted_count` via
  `on_events(agent_id, pending)` — same decoupling as Claude (HTTP reads and the poll
  loop share the offset; the loop broadcasts by `emitted_count`, not by what it
  parsed).

### event_id (spine invariant: stable, harness-derived)
```
event_id = f"{_mngr_conv_id}:{step_index}:{suffix}"
suffix ∈ {"user", "assistant", "toolcall-<n>", "error"}
```
Because one PLANNER_RESPONSE record yields an `assistant_message` **plus** N
`tool_call` entries embedded in it, the assistant event is one event; tool calls are
carried inside its `tool_calls[]` (as in Claude), not separate events. `step_index`
is unique within a conversation and monotonic; `_mngr_conv_id` disambiguates resumed
conversations sharing the file.

### Ordering
Sort emitted events by `(_mngr_conv_id groupings in first-seen order, step_index)`.
In practice a single agent works one conversation at a time and the decoder writes in
`idx` order, so file order already equals render order; sort by file order and keep
`step_index` only as a tiebreak. Do **not** sort by `created_at` (second-granularity,
many ties).

### The 11 abstract methods
- `get_all_events / get_tail_events / get_backfill_events / get_forward_events /
  get_events_at_offset / get_event_offset / get_total_event_count` — index into the
  ordered locator list exactly like Claude's single-session case.
- `get_subagent_metadata` → `None`; `is_main_session_event` → `True` (agy has no
  subagents in this stream).
- `start/stop` — schedule/stop the watchdog + poll thread.

---

## 4. `session_parser.py` — record → shared UI events

Pure function `parse_records(records, existing_event_ids) -> list[event dict]`,
mirroring `claude/session_parser.parse_lines`. Emits the shared `events.py` shapes so
the frontend renders unchanged. Dedup by `event_id` against `existing_event_ids`.

Mapping:

| record.type | source | → event |
|---|---|---|
| `USER_INPUT` | USER_EXPLICIT | `user_message` (see text extraction) |
| `USER_INPUT` | USER_IMPLICIT / SYSTEM* | **skip** (framework-injected context, not a user turn) |
| `PLANNER_RESPONSE` | MODEL | `assistant_message` with `text`, optional `thinking`, and `tool_calls[]` |
| `ERROR_MESSAGE` | any | `assistant_message` with `is_api_error`/error text, empty `tool_calls` |
| `CONVERSATION_HISTORY`, `SYSTEM_MESSAGE`, `STEP_TYPE_<n>`, anything else | any | **skip** |

### `user_message`
`content` is wrapped by agy as
`"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>…"` (verified). Strip
to the inner `<USER_REQUEST>` body; drop the metadata/settings-change trailers. If no
`<USER_REQUEST>` wrapper (older records), use `content` verbatim. Emit:
`{type:"user_message", event_id, role:"user", content, source:"antigravity/common_transcript", message_uuid: event_id, timestamp: created_at}`.

### `assistant_message`
`{type:"assistant_message", event_id, role:"assistant", model: <agy model or "">,
text: content, thinking: record.thinking or "", tool_calls: [...], stop_reason: null,
timestamp, message_uuid: event_id, source}`.
- **thinking**: unlike Claude (which drops thinking), agy gives us `thinking` — pass
  it through on the event. Frontend already tolerates unknown fields; rendering a
  collapsed "thought" is a small frontend add (optional, not required for v1).
- Each `record.tool_calls[i] = {name, args}` → a `tool_call` dict:
  ```
  {tool_call_id: f"{event_id}-toolcall-{i}",
   tool_name: name,
   input_preview: <args clipped to MAX_TOOL_INPUT_PREVIEW_LENGTH>,
   header_label, caption_label}     # from tool_labels(name, args) — §6
  ```
  `args` is already a JSON string; clip to `events.MAX_TOOL_INPUT_PREVIEW_LENGTH`
  (200) like Claude, then feed the raw (unclipped) args to `tool_labels` for target
  extraction.

### No `tool_result` events (design limitation, call it out)
mngr's stream drops tool-result steps, so we emit tool calls but **no results**. The
UI shows "used `run_command`: Running python3 showcase.py" with no output body. This
is acceptable for v1 (user asked for tool-call detection + title). Wiring results
would require tail source B (the `.db`), where the result lives in the tool step's
own body field — a later enhancement.

Truncation constants: reuse `events.MAX_TOOL_INPUT_PREVIEW_LENGTH` /
`MAX_TOOL_OUTPUT_LENGTH` for cross-harness consistency.

---

## 5. Activity — `activity.py` + `activity_state.py`

**The current placeholder `activity.py` reuses Claude's `has_unmatched_tool_use`,
which is WRONG for agy**: we emit tool calls but never tool results, so every tool
call is forever "unmatched" → the tracker would pin `TOOL_RUNNING` permanently. Replace
the derivation.

agy has no turn-boundary markers, and — critically — no in-flight step is persisted
(the running tool has no row until it completes). So the only reliable signals are the
**mngr lifecycle** (is the agent process mid-turn) and the **transcript tail shape**.

### `activity_state.py` (new, pure) — `derive(...)`
Ladder (priority order):
1. `if not is_agent_running: return IDLE` — a non-RUNNING mngr agent is IDLE.
2. `if is_transcript_tail_stale(tail_event_at, process_started_at): return IDLE` —
   reuse the shared staleness guard (tail predates the current process = abandoned
   mid-turn after a restart). Uses the `antigravity_process_started` marker mtime.
3. Determine the tail settled step:
   - tail is `assistant_message` **with text and no tool_calls** → the model gave its
     final answer → `IDLE`.
   - tail is `assistant_message` **with tool_calls** (model just dispatched tools; the
     results/continuation haven't landed) → `TOOL_RUNNING`.
   - tail is `user_message` (agent handed input, hasn't answered) → `THINKING`.
   - empty transcript but process running → `THINKING` (turn started, nothing settled
     yet — agy writes the first step only on completion).
4. else `IDLE`.

This mirrors Claude's tail logic but keys on "assistant_message with vs without
tool_calls" instead of unmatched-tool-use, since we have no results. Three states as
today: `IDLE / THINKING / TOOL_RUNNING`.

### `activity.py` (rewrite `observe`)
Cache: `_last_event_type`, `_last_event_has_tool_calls`, `_last_event_timestamp`.
`observe` recomputes them from the tail event; return `True` iff any changed. `derive`
forwards them + `is_agent_running` + `process_started_at` to the pure function.
Keep `marker_filename = "antigravity_process_started"` (already correct; mngr touches
it — verify the mngr plugin writes exactly this name, else the staleness guard is
inert).

Optional refinement (defer): the decoder exposes `status` (RUNNING/GENERATING/…). If a
future tail source surfaces a non-terminal step, prefer it as a direct busy signal.
Not available from source A today.

---

## 6. `tool_labels.py` — tool-call detection & titles

Same contract as `claude/tool_labels.py`: `tool_labels(tool_name, args_json) ->
(header_label, caption_label)`, attached to each tool_call in the parser. agy's own
`f30/f31` captions are **not** in source A, so we synthesize from name+args (agy's tool
names are descriptive and its args are a JSON object with well-known keys — verified
against real steps).

`header_label = f"Tool: {tool_name}"` (agy reports real names, no translation table).

`caption_label`: verb from a table + target from args. Verb table (from observed agy
tools):

| agy tool_name | verb | target key(s) in args |
|---|---|---|
| `write_to_file` | Writing | `TargetFile` → basename |
| `replace_file_content`, `multi_replace_file_content` | Editing | `TargetFile` → basename |
| `view_file` | Viewing | `AbsolutePath` → basename |
| `list_dir` | Listing | `DirectoryPath` → shorten |
| `grep_search` | Searching | `Query` → quoted |
| `find_by_name`, `file_glob` | Searching | `Pattern`/`Query` → quoted |
| `run_command` | Running | `CommandLine` → shorten (fallback for target) |
| `read_url_content`, `read_web_page` | Fetching | `Url` → shorten |
| `search_web` | Searching the web | `Query` → quoted |
| `generate_image` | Generating image | `ImageName`/`Prompt` → shorten |
| `browser_*` | Browsing | url/selector → shorten |

Resolution helpers: reuse the **shared** `harnesses/tool_labels.py`
(`basename`, `shorten`, `quoted`, `parse_input_preview`, `mcp_caption`,
`first_string_value`) — do not reimplement. Unknown tool → `caption_label =
f"Running {target}"` or `GENERIC_CAPTION = "Running tool…"`. args is real JSON here
(not always truncated), so `json.loads(args)` succeeds more often than Claude's
preview-parse; still fall back to `{}` on failure.

Note: agy also ships nicer captions natively (`f30`="Writing showcase.py",
`f31`="Creating showcase python file"). If we ever move to source B, prefer those
verbatim over synthesizing.

---

## 7. Event schema — worked examples

Real records (captured live), showing the mapping:

```
{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE",
 "content":"<USER_REQUEST>\nput on a little tool call show…\n</USER_REQUEST>…"}
→ user_message  content="put on a little tool call show…"

{"step_index":15,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE",
 "content":"", "tool_calls":[{"name":"run_command","args":"{\"CommandLine\":\"python3 /home/user/showcase.py\",…}"}]}
→ assistant_message text="" tool_calls=[{tool_name:"run_command",
     header_label:"Tool: run_command", caption_label:"Running python3 /home/user/showcase.py"}]

{"step_index":19,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE",
 "content":"Here is the tool call show…","thinking":"**Initiating…"}
→ assistant_message text="Here is the tool call show…" thinking="**Initiating…" tool_calls=[]
```

Activity over that turn: user_message tail → THINKING; PLANNER_RESPONSE-with-tool_calls
tail → TOOL_RUNNING; final PLANNER_RESPONSE-text-no-tool_calls tail → IDLE.

---

## 8. Testing plan

Unit tests with inline record fixtures (no live agy needed) — mirror
`claude/session_parser_test.py`:
- `session_parser_test.py`: USER_INPUT wrapper stripping; USER_IMPLICIT skip;
  PLANNER_RESPONSE text+thinking+tool_calls; ERROR_MESSAGE; unknown type skip; dedup by
  event_id; multi-tool PLANNER_RESPONSE yields N tool_calls with distinct ids.
- `tool_labels_test.py`: each verb-table row; unknown tool; malformed args → `{}`;
  basename/quoted/shorten targets.
- `activity_state_test.py`: the four ladder cases + non-running IDLE + stale-tail IDLE;
  the tool-calls-vs-no-tool-calls distinction (the bug the reuse would have caused).
- `watcher_test.py`: byte-offset incremental read across appends; mid-write partial
  line held then completed; event_id stability across re-read; paging
  (tail/backfill/forward/offset). Real captured records make good fixtures.

Manual (tmux): create an agy agent, drive a multi-tool task, confirm messages + tool
titles render and the indicator goes THINKING → (tool) → IDLE. Do **not** crystallize
tmux checks into pytest.

Ratchets: `read_json_dict` for any JSON reads (avoid the bare-`json.loads` ratchet);
`dict[str, Any]` not bare `dict`.

---

## 9. Out of scope / limitations (explicit)

- **Queuing: not implemented.** Verified live that a message typed while agy is busy is
  **not** written to the transcript while queued (it lives only in agy's TUI, shown
  with `▸`); it lands as an ordinary `USER_INPUT` step only once agy dequeues it, with
  no queued marker. There is no Claude-style `queued_command` record to parse. Per
  direction, ship without queuing; do not rely on `PendingMessages` for agy.
- **No tool results** in v1 (source A drops them). Tool calls show title only.
- **No native captions** in v1 (synthesized from name+args; source B has agy's own).
- **~1s render lag** from the mngr streamer poll (busy/idle indicator is unaffected —
  it's lifecycle-driven).
- **No subagents** surfaced.
- **Schema drift**: owned by mngr's descriptor-diff test; if mngr's record shape
  changes, update the parser. system_interface never touches agy's protobuf.

---

## 10. Build order

1. `tool_labels.py` + test (pure, no deps).
2. `session_parser.py` + test (pure; consumes records, emits events).
3. `activity_state.py` + `activity.py` rewrite + test (fixes the stuck-TOOL_RUNNING bug).
4. `watcher.py` real impl + test (JSONL tail → parser → on_events).
5. Manual tmux verification end-to-end; confirm the mngr plugin actually writes
   `antigravity_process_started` and populates `logs/antigravity_transcript/events.jsonl`
   for a live agent (both observed to exist; confirm they fill during a turn).
```
