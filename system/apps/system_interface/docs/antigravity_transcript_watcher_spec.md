# Antigravity (agy) transcript harness — implementation spec

Scope: everything in `imbue/system_interface/harnesses/antigravity/` needed to turn
the placeholder into a real transcript harness — message rendering, tool-call
detection + titles, tool results, the activity indicator, and the activity caption.
Modeled on the Claude/Codex harnesses. **Queuing is out of scope** (§9).

Status quo: model bar fully wired (`model.py`, read-only catalog + resolver).
`watcher.py`/`activity.py` are placeholders. Registry entry exists
(`special_kinds=frozenset()`). This spec fills in a real watcher that reads **agy's own
transcript**, a parser, tool labels, and activity.

---

## 0. Architecture rule (the correction that governs this spec)

**A system_interface harness reads the agent's OWN original transcript, directly —
never mngr's mirror.** The Claude watcher tails `~/.claude/projects/**/<session>.jsonl`;
the Codex watcher tails its live rollout and explicitly does **not** read
`mngr_codex`'s `stream_transcript.sh` mirror (the mirror lags). Antigravity follows
the same rule: it reads agy's authoritative conversation store.

`mngr_antigravity` has its own decoder (`decode_agy_transcript.py`) feeding
`mngr transcript` — that is mngr's concern, not ours. We do **not** consume
`logs/antigravity_transcript/events.jsonl`. We decode agy's store ourselves so we get
full fidelity (tool results, args, and agy's native captions) that mngr's stream drops.

### agy's original store
Since agy 1.0.4 each conversation is a **protobuf-encoded SQLite DB**:
```
<agent_state_dir>/plugin/antigravity/home/.gemini/antigravity-cli/conversations/<conv_id>.db
```
Table `steps`: `idx` (int PK, order), `step_type` (int enum), `status` (int enum),
`step_payload` (BLOB = a `gemini_coder.Step` protobuf), `step_format`. This is what agy
reads back on resume — the authoritative original, parallel to Claude's session JSONL.

**Fidelity is proven.** A dependency-free wire-walk over `step_payload` (in-process,
no mngr) already yields, per tool step: call id, tool name, args JSON, agy's native
short caption (`Running python3 showcase.py`) and long caption, and the tool result
text. Verified against real `.db`s.

---

## 1. What the watcher reads and how

### Conversation discovery (like Claude's session-history)
`<agent_state_dir>/antigravity_conversation_ids` — one conversation UUID per line,
maintained by mngr's `PreInvocation` capture hook (verified present on live agents).
Resumed agents accumulate multiple ids; render them in file order (matches chronology,
like Claude concatenating its session history). For each id, open
`…/conversations/<id>.db`.

`AgentInfo` exposes `agent_state_dir` (Codex uses it the same way). Derive:
```
agy_home        = agent_state_dir / "plugin/antigravity/home"
conversations   = agy_home / ".gemini/antigravity-cli/conversations"
conv_ids_file   = agent_state_dir / "antigravity_conversation_ids"
```

### Tailing = SQLite row-offset polling (NOT byte-offset)
This is the one structural difference from Claude/Codex. There is no append-JSONL to
seek in; instead:
- Keep an **in-memory** last-seen `idx` per `conv_id` (our own offset — do **not** read
  mngr's `.transcript_offsets/`, that's mngr's cursor at mngr's pace).
- Each poll, per conversation:
  ```
  SELECT idx, step_type, status, step_payload FROM steps WHERE idx > ? ORDER BY idx
  ```
  Open **read-only, WAL-aware**: `sqlite3.connect(f"file:{db}?mode=ro", uri=True)`.
  NOT `immutable=1` (agy is concurrently writing WAL). A transient `sqlite3.Error`
  (locked/mid-checkpoint) → skip this conversation this pass, retry next.
- **Stop at the first non-terminal (still-generating) row** and do not advance the
  offset past it — its payload is incomplete. Terminal statuses = `{DONE, INVALID,
  CLEARED, CANCELED, ERROR, INTERRUPTED}` (the same set mngr's decoder uses). This
  preserves in-order, no-partial, no-skip emission.
- Decode each settled row → events; broadcast via `on_events`.

### Wake / poll
Watchdog on the `conversations/` dir (catches `.db`/`-wal` writes) + 1s poll safety net
(`watcher_common.POLL_INTERVAL_SECONDS`, `WakeOnChangeHandler`) — same primitives as
Claude, just triggering a DB re-query instead of a file re-read.

### event_id (spine invariant: stable, harness-derived)
```
f"{conv_id}:{idx}:{suffix}"   suffix ∈ {user, assistant, toolcall, toolresult, error}
```
A tool step yields two events (a `tool_call` carrier + a `tool_result`) sharing the
`idx`, distinguished by suffix. `idx` is unique+monotonic within a conversation;
`conv_id` disambiguates resumed conversations.

### The 11 abstract methods
Index into the ordered decoded-step list exactly like Claude's single-session case
(`get_all/tail/backfill/forward/at_offset/offset/total`). `get_subagent_metadata` →
`None`; `is_main_session_event` → `True` (no subagents in the store). `start/stop`
manage the watchdog + poll thread.

---

## 2. Files in `harnesses/antigravity/`

```
agy_transcript.py       NEW  protobuf wire-walk: steps row → decoded Step record (full fidelity)
watcher.py              REPLACE placeholder → sqlite-tailing watcher (uses agy_transcript)
session_parser.py       NEW  decoded Step record → shared UI events
tool_labels.py          NEW  thin: uses agy's native captions (no synthesis)
activity.py             KEEP claude-style (now correct — see §5)
activity_state.py       —    reuse claude/activity_state (see §5); no new file
model.py, icon.svg, __init__.py   KEEP
*_test.py               NEW  agy_transcript_test, session_parser_test, tool_labels_test, watcher_test
```

Registry: entry present; keep `special_kinds=frozenset()`. No spine changes.

---

## 3. `agy_transcript.py` — the protobuf decoder (ported + extended)

A small, dependency-free protobuf wire-walk. **Port it from
`mngr_antigravity/resources/decode_agy_transcript.py`** (proven, defensive: truncation
guards, `utf-8 "replace"`, unknown-field skip) but **extend** it to surface what mngr
drops: tool call id/args, agy's captions, and tool results.

### `gemini_coder.Step` field map (recovered; keep in sync with mngr — §8)
Top-level `step_payload`:
- `f1` step_type (varint) — mirrors the `step_type` column
- `f4` status (varint)
- `f5` metadata (message)
- body oneof by type: `f10` code_action · `f19` user_input · `f20` planner_response ·
  `f24` error_message

`metadata` (f5):
- `f1` created_at `Timestamp{f1 seconds, f2 nanos}`
- `f3` source (varint)
- `f4` **ChatToolCall** `{f1 call_id, f2 name, f3 args(JSON string), f9 name}` — present
  on tool steps
- `f30` caption_short (e.g. `"Running python3 showcase.py"`)
- `f31` caption_long (e.g. `"Executing showcase python script"`)

Bodies:
- user_input (f19): `f1` query or `f2` user_response = the text
- planner_response (f20): `f1` text · `f3` thinking · `f7` repeated ChatToolCall
- error_message (f24): `f3` CortexErrorDetails `{f1 user_msg, f2 short, f3 full}`

### Enums (from mngr's recovered map)
- step_type: `5 CODE_ACTION, 14 USER_INPUT, 15 PLANNER_RESPONSE, 17 ERROR_MESSAGE,
  98 CONVERSATION_HISTORY, 101 SYSTEM_MESSAGE`, plus per-tool categories observed in the
  wild: `7 GREP_SEARCH, 8 VIEW_FILE, 9 LIST_DIRECTORY, 21 RUN_COMMAND, 91 GENERATE_IMAGE`
  (unknown → `STEP_TYPE_<n>`).
- source: `2 MODEL, 3 USER_IMPLICIT, 4 USER_EXPLICIT, 5 SYSTEM, 6 SYSTEM_SDK`.
- status: `1 PENDING, 2 RUNNING, 3 DONE, 4 INVALID, 5 CLEARED, 6 CANCELED, 7 ERROR,
  8 GENERATING, 9 WAITING, 11 QUEUED, 12 INTERRUPTED`. Terminal = `{3,4,5,6,7,12}`.

### Tool-step detection (robust to new tool types)
**A step is a tool call iff `metadata.f4` exists and has a name (f2).** Do not enumerate
tool step_types — new agy tools get new type numbers but keep the same `metadata.f4`
ChatToolCall shape. From it: `call_id=f1, name=f2, args=f3`, captions `f30/f31`.

### Tool result extraction
The result lives in a per-tool-type top-level body field (observed: write→f10,
grep→f13, view→f14, list_dir→f15, run_command→f28, image→f104). Two-tier approach:
1. Map known step_type → result field for precise extraction (edit diff, grep hits,
   command stdout, dir listing).
2. Fallback for unknown tools: the longest printable string across non-metadata
   top-level fields (proven to recover run_command output, grep results, edit diffs).
Truncate to `events.MAX_TOOL_OUTPUT_LENGTH` (2000).

### Output: a decoded record dataclass
```
DecodedStep(conv_id, idx, step_type_name, status_name, created_at, source,
            text, thinking, user_text,
            tool_call: {call_id, name, args, caption_short, caption_long} | None,
            tool_result_text: str | None,
            error_text: str | None)
```
`_TruncatedError` on a mid-write payload → caller stops at that step (as in mngr's).

---

## 4. `session_parser.py` — record → shared UI events

`parse_steps(records, existing_event_ids) -> list[event dict]`, dedup by `event_id`,
emitting the shared `events.py` shapes so the frontend renders unchanged.

| record | → events |
|---|---|
| `USER_INPUT`, source USER_EXPLICIT | `user_message` (strip agy's `<USER_REQUEST>…</USER_REQUEST>` wrapper; drop metadata trailers) |
| `USER_INPUT`, source USER_IMPLICIT/SYSTEM | skip (injected context, not a turn) |
| `PLANNER_RESPONSE` with text | `assistant_message` {text, thinking, tool_calls:[]} |
| tool step (metadata.f4 present) | `assistant_message` {text:"", tool_calls:[one tool_call]} **plus** a `tool_result` {tool_call_id, output} — both from the same step, sharing call_id |
| `ERROR_MESSAGE` | `assistant_message` flagged error (is_api_error/text) |
| everything else | skip |

- **tool_call** dict: `{tool_call_id: f"{conv_id}:{idx}:toolcall", tool_name: name,
  input_preview: <args clipped to 200>, header_label, caption_label}` (labels from §6).
- **tool_result** dict: `{type:"tool_result", event_id: f"{conv_id}:{idx}:toolresult",
  tool_call_id: <same>, tool_name, output: <result clipped 2000>, is_error: status∈{ERROR,CANCELED}}`.
- **thinking**: pass agy's `thinking` through on the assistant event (Claude drops
  thinking; we get it free). Rendering it collapsed is a small optional frontend add.
- Emitting call+result from one step means the pair is **always matched** — this is
  what makes activity (§5) correct.

`source` field on events: `"antigravity/steps"`; `message_uuid = event_id`;
`timestamp = created_at`.

---

## 5. Activity — reuse Claude's derivation (now valid)

The placeholder `activity.py` already reuses `claude/activity_state.derive` +
`has_unmatched_tool_use`. **That is correct now** — because §4 emits a `tool_result`
for every tool call, tool calls get matched, so `has_unmatched_tool_use` is only
transiently true and never stuck. (An earlier draft of this spec, when it planned to
read mngr's lossy stream that drops results, hit permanent TOOL_RUNNING; emitting
results from the same step fixes it and is the reason to read the original store.)

Derivation (Claude's ladder, unchanged):
- not RUNNING (mngr lifecycle) → IDLE
- transcript tail stale vs `antigravity_process_started` marker mtime → IDLE
- unmatched tool call at tail → TOOL_RUNNING
- tail is user_message/tool_result → THINKING
- tail is assistant_message (final answer) → IDLE

Keep `marker_filename = "antigravity_process_started"`; **verify the mngr plugin writes
exactly that filename** on launch/resume, else the staleness guard is inert.

Caveat (document, don't fix): agy writes a step row only on completion (the in-flight
tool has no row until it finishes — confirmed live). So between "tool dispatched" and
"tool done" there is no row; the indicator rides the mngr lifecycle (RUNNING) and the
prior tail. Acceptable — the busy signal is lifecycle-anchored.

Activity **caption**: the frontend renders the in-flight tool's `caption_label`. Since
we carry agy's native caption on the tool_call, it reads well ("Running python3
showcase.py"). No tool in flight → THINKING → "Thinking…".

---

## 6. `tool_labels.py` — use agy's native captions (no synthesis)

Unlike Claude (which synthesizes captions from tool name + args), **agy already provides
them** in `metadata.f30/f31`. So:
- `caption_label = caption_short (f30)` — agy's own short caption.
- `header_label  = f"Tool: {tool_name}"` (or `caption_long (f31)` if we prefer agy's
  long form as the header).
- Fallback only if f30 is empty: synthesize via the **shared** `harnesses/tool_labels.py`
  (`basename`/`shorten`/`quoted` + a small verb table: write_to_file→Writing,
  run_command→Running, grep_search→Searching, view_file→Viewing, list_dir→Listing,
  replace_file_content→Editing, generate_image→Generating image). Reuse the shared
  helpers; don't reimplement.

This makes `tool_labels.py` thin — mostly "prefer f30, fall back to synthesis."

---

## 7. Worked example (real `.db`, decoded in-process)

```
steps.idx=0  type=USER_INPUT     → user_message "put on a little tool call show…"
steps.idx=3  type=LIST_DIRECTORY → assistant_message(tool_call list_dir, cap "Listing directory /home/user")
                                    + tool_result(output=dir listing)
steps.idx=6  type=CODE_ACTION    → tool_call write_to_file cap "Writing showcase.py" + tool_result(diff)
steps.idx=16 type=RUN_COMMAND    → tool_call run_command cap "Running python3 showcase.py"
                                    + tool_result("✨ Hello from Antigravity!… Result of magic calculation: 1000")
steps.idx=19 type=PLANNER_RESPONSE → assistant_message "Here is the tool call show…" (+thinking)
```
Activity: user tail → THINKING; each tool call+result matched → brief TOOL_RUNNING then
THINKING; final planner answer → IDLE.

---

## 8. Schema-drift obligation (the one real cost of full fidelity)

agy ships no `.proto`; the field/enum numbers are recovered from the binary. mngr guards
this with a **release-marked descriptor-diff test** and a regeneration doc
(`libs/mngr_antigravity/regenerating_protobuf_schema.md`). By decoding in system_interface
we take on a second copy of that map. Mitigations, in preference order:
1. **Share, don't duplicate**: factor the field-number/enum constants + wire-walk into
   one importable module both mngr and system_interface use. Check whether
   system_interface may depend on `mngr_antigravity` (it already reads mngr's agent dirs);
   if so, import the decoder and *extend* via a thin wrapper for the extra fields.
2. If not importable: vendor a copy into `agy_transcript.py`, add a parallel
   descriptor-diff test in system_interface, and cross-reference mngr's regeneration doc
   so an agy bump updates both. Document the sync obligation at the top of the file.

Do not silently fork the map with no guard.

---

## 9. Out of scope / limitations (explicit)

- **Queuing: not implemented.** Verified live: a message typed while agy is busy is
  **not** written to the store while queued (lives only in agy's TUI, shown `▸`); it
  lands as an ordinary `USER_INPUT` step only once dequeued, with no queued marker. No
  Claude-style `queued_command` to parse. Ship without it; do not rely on `PendingMessages`.
- **In-flight tool not persisted** — no row until the tool completes; busy/idle rides the
  lifecycle (§5). No "running THIS tool" caption mid-execution beyond the last dispatch.
- **No subagents** in the store.
- **Schema drift** — must be guarded (§8).

---

## 10. Testing plan

Unit (inline fixtures built from real captured `.db` bytes — commit a few real
`step_payload` blobs as testdata):
- `agy_transcript_test.py`: decode user/planner/tool/error steps; tool detection via
  metadata.f4; caption + args + result extraction; truncated payload → `_TruncatedError`;
  unknown step_type/tool → graceful skip/fallback; unknown enum → `STEP_TYPE_<n>`.
- `session_parser_test.py`: USER_REQUEST unwrap; USER_IMPLICIT skip; planner
  text+thinking; tool step → matched assistant+tool_call+tool_result with shared id;
  error; dedup by event_id.
- `tool_labels_test.py`: prefer f30; fallback synthesis per verb; empty f30.
- `watcher_test.py`: sqlite row-offset polling advances on new rows; stops at
  non-terminal status; WAL `mode=ro` open; multi-conversation concat in
  `antigravity_conversation_ids` order; paging methods.

Manual (tmux, not crystallized): create an agy agent, drive a multi-tool task, confirm
messages + tool titles + results render and the indicator goes THINKING → (tool) → IDLE.

Ratchets: `read_json_dict` / `dict[str, Any]`.

---

## 11. Build order

1. `agy_transcript.py` + test — port mngr's wire-walk, extend for tool call/args/captions/result.
2. `session_parser.py` + test — records → events (call+result matched).
3. `tool_labels.py` + test — prefer agy captions.
4. `watcher.py` — sqlite row-offset tail → parser → on_events; conversation discovery.
5. `activity.py` — already correct once results are emitted; verify end-to-end.
6. Manual tmux verification; confirm the mngr plugin writes `antigravity_process_started`
   and that `antigravity_conversation_ids` + `conversations/<id>.db` populate live.
```
