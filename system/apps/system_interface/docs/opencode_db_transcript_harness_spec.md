# opencode transcript harness — tail opencode's own SQLite DB

Status: **design.** The opencode model bar is shipped (`harnesses/opencode/model.py`,
`probe.py`, catalog, live switch). The transcript is **not**: `registry.py` still wires
`OpenCodePlaceholderSessionWatcher` (emits nothing), so an opencode tab renders an empty
conversation. This spec designs the real watcher/parser/labels, and — per the explicit
decision below — tails **opencode's own `opencode.db`**, not the mngr plugin's mirror jsonl.

Legend: `[NEW]` create, `[EDIT]` modify, `[DONE]` shipped, `Q:` decide while coding.
Reference implementation for every DB-tailing piece is the **antigravity** harness
(`harnesses/antigravity/`), which already tails a live SQLite store the same way. The
event/watcher contracts are `harnesses/events.py` and `harnesses/session_watcher.py`; the
label contract is `harnesses/tool_labels.py`.

Grounded against the installed opencode (Bun binary at `~/.opencode/bin/opencode`), its
live `opencode.db` schema, real preserved-agent conversation rows, and the mngr_opencode
plugin (`system/vendor/mngr/libs/mngr_opencode/`).

---

## 0. The decision: tail the DB, not the plugin jsonl

The mngr_opencode plugin (`resources/mngr_opencode_plugin.ts`) already writes two feeds:

- `logs/opencode_transcript/events.jsonl` — raw `{type, properties}` per
  `message.updated`/`message.part.updated`, append-only.
- `events/opencode/common_transcript/events.jsonl` — the agent-agnostic common
  transcript, rebuilt **only on `session.idle`** (once per turn).

The existing plan (`opencode_pi_harness_spec.md` §C) proposed tailing the raw jsonl. This
spec overrides that: **tail `opencode.db` directly.** Rationale:

1. **Single source of truth.** The DB is opencode's own store — the same file adopt/merge/
   resume already read (`opencode_config.py`). The plugin jsonl is a mngr-added mirror that
   can drift, lag, or be absent (it only exists in the `MNGR_OPENCODE_ROLE=server` process,
   and the common one is idle-latched). Reading the DB removes that dependency entirely.
2. **Liveness.** Rows are written/updated as a message streams (a text part's `text` grows,
   a tool part goes `running`→`completed`), so tailing the DB gives the same live
   "thinking → streaming → tool running → result" feel the raw jsonl does, without waiting
   for `session.idle` (which the common jsonl needs).
3. **Exact parallel to antigravity**, which already tails a live SQLite conversation store
   with the settling/dedup machinery this needs. We reuse that shape rather than invent one.

The plugin's `buildCommonRecords()` (`mngr_opencode_plugin.ts:289-372`) is nonetheless the
**authoritative reference for the DB→event mapping** — the parser below reproduces its logic
(same `event_id`s, same fields), only sourcing ids from DB columns instead of SDK event
properties. This keeps the DB-tail and `mngr transcript` renderings identical.

`opencode_model_state.json` (model bar) and the `active`/`permissions_waiting` markers
(lifecycle) are unaffected — this spec touches only the transcript surface.

---

## 1. On-disk facts (verified live)

**DB location** (relative to `agent_info.agent_state_dir`):
`plugin/opencode/data/opencode/opencode.db` (+ `-wal`, `-shm` sidecars). This is
`XDG_DATA_HOME=<state>/plugin/opencode/data` → opencode namespaces under `opencode/`.
Mirrors `opencode_config.NATIVE_DB_RELATIVE_PATH` / `get_opencode_db_path_for_data_home`.
The `model.py` resolver already hardcodes `_DATA_HOME_RELPATH = ("plugin","opencode","data")`.

**WAL mode.** The db is SQLite in WAL mode; uncheckpointed writes live in the `-wal` sidecar.
Open read-only via URI (`file:<path>?mode=ro`, `uri=True`) — sqlite reads the WAL
automatically. `opencode_config.py` already opens it exactly this way (`_db_has_session`).

**Root session id**: `<state>/opencode_root_session` (`ROOT_SESSION_FILENAME`), a bare
`ses_...` string, written by `opencode_launch.sh`. `model.py._read_root_session_id` already
reads it. Subagent (task-tool) sessions are separate `session` rows with `parent_id` set.

**Three-table conversation store** (schema dumped live; `data` is a JSON blob):

```sql
message(id TEXT PK,           -- msg_...
        session_id TEXT,      -- FK session(id)
        time_created INTEGER, -- ms epoch
        time_updated INTEGER, -- ms epoch, advances on every in-place update
        data TEXT)            -- JSON, see below
part(id TEXT PK,              -- prt_...
     message_id TEXT,         -- FK message(id)
     session_id TEXT,
     time_created INTEGER,
     time_updated INTEGER,
     data TEXT)               -- JSON, see below
-- index message_session_time_created_id_idx (session_id, time_created, id)
-- index part_message_id_id_idx (message_id, id)
```

Note: the id / session_id / message_id / timestamps are **columns**; the JSON `data` does
NOT repeat them (unlike the plugin's SDK-event `properties.info`/`properties.part`, which
inline the id). The parser must combine column ids with the `data` object.

**`message.data` shapes** (real rows):

```jsonc
// role=user
{ "role":"user", "time":{"created":1786109097033}, "agent":"build",
  "model":{"providerID":"opencode","modelID":"deepseek-v4-flash-free"}, "summary":{...} }
// role=assistant
{ "parentID":"msg_...", "role":"assistant", "mode":"build", "agent":"build",
  "path":{"cwd":"...","root":"..."}, "cost":0,
  "tokens":{"total":..,"input":..,"output":..,"reasoning":..,"cache":{"read":..,"write":..}},
  "modelID":"deepseek-v4-flash-free", "providerID":"opencode",
  "time":{"created":..,"completed":..}, "finish":"stop" }
```

`finish`/`time.completed` are present only once the assistant message settles. `variant`
(opencode's effort axis) is NOT on `message.data` in the installed version — it is on the
`session.model` JSON — so the transcript does not carry it (the model bar reads it elsewhere).

**`part.data` shapes** (real rows; `type` discriminates):

```jsonc
{ "type":"text", "text":"...", "time":{"start":..,"end":..} }          // may add "synthetic":true
{ "type":"reasoning", ... }                                             // opencode's thinking
{ "type":"step-start", "snapshot":"<sha>" }                            // turn bookkeeping
{ "type":"step-finish", "reason":"stop", "tokens":{...}, "cost":0 }    // turn bookkeeping
{ "type":"tool", "tool":"bash", "callID":"01KZ...",
  "state":{ "status":"completed",                                      // pending|running|completed|error
            "time":{"start":..,"end":..},
            "input":{"command":"ls"},
            "title":"", "metadata":{"output":"..."},
            "output":"AGENTS.md\n...",                                  // present on completed
            "error":"..." } }                                          // present on error
```

Only `text` and `tool` parts become events (parity with `buildCommonRecords`); `step-*` are
skipped; `reasoning` is skipped in v1 (see Q4). Distinct part types seen live: `text`,
`reasoning`, `tool`, `step-start`, `step-finish`.

**In-place mutation.** As a turn runs, opencode UPDATES rows: a streaming `text` part's
`text` grows, a `tool` part's `state` goes `pending`→`running`→`completed`/`error`, and the
assistant `message.data` gains `finish`/`time.completed`. Each write bumps that row's
`time_updated`. This is the same "row settles in place" behavior antigravity's cursor logic
is built around.

---

## 2. `[NEW] harnesses/opencode/db_reader.py` — the SQLite → typed-row layer

Antigravity splits DB access (`watcher.py`) from decode (`agy_transcript.py`) from event
mapping (`session_parser.py`). opencode is simpler (JSON, not protobuf), so this layer just
reads rows and `json.loads` the `data` column into typed models. Pure/deterministic and
unit-testable against a fixture db.

Public surface:

- `OPENCODE_DB_RELPATH: Path` — `plugin/opencode/data/opencode/opencode.db` (mirror the
  `opencode_config` literal locally; do not import the plugin — same reimplement-don't-import
  stance codex/claude take, since those are separate packages).
- `ROOT_SESSION_RELPATH` — `opencode_root_session`.
- `OpenCodeMessage(FrozenModel)` — `id, session_id, time_created, time_updated, role,
  provider_id, model_id, finish, parent_id` (parsed from column + `data`).
- `OpenCodePart(FrozenModel)` — `id, message_id, session_id, time_created, time_updated`,
  and the type-specific payload: `kind` (`"text"|"tool"|"reasoning"|"other"`), `text`,
  `synthetic`, `tool_name`, `call_id`, `state_status`, `state_input` (dict), `state_output`,
  `state_error`.
- `read_root_and_descendant_session_ids(db_path, root_session_id) -> set[str]` — the root
  plus every session whose `parent_id` chain reaches it (recursive CTE, as
  `build_opencode_merge_sql` walks). For v1 filtering see Q3.
- `read_changed_messages(db_path, session_ids, since_updated) -> tuple[list[OpenCodeMessage],
  dict[str, list[OpenCodePart]]]` — the incremental read (see §3 for the query + cursor
  contract). Returns messages whose own or whose child part's `time_updated >= since_updated`,
  each with ALL its current parts (a message's events depend on all its parts, so a message is
  re-emitted whole whenever any part changes).

Robustness (mirror antigravity/`opencode_config._db_has_session`): every connect + query in
`try/except sqlite3.Error` → return "nothing this pass" so a transient WAL lock/checkpoint is
retried next poll, never raised. Open read-only (`file:...?mode=ro`, `uri=True`). A row whose
`data` is not valid JSON is logged-and-skipped (like the codex parser's malformed-line path),
not fatal.

---

## 3. `[NEW] harnesses/opencode/watcher.py` — `OpenCodeDbSessionWatcher`

`AgentSessionWatcher` subclass; the opencode analogue of `AntigravitySessionWatcher` /
`CodexSessionWatcher`. Same construction idiom (`build` classmethod + `cls.__new__`, no
`__init__`), same in-memory store, same read API.

### State (from codex/antigravity, adapted)
- `_agent_id`, `_db_path` (`<state>/plugin/opencode/data/opencode/opencode.db`),
  `_root_session_path` (`<state>/opencode_root_session`), `_on_events`, `_lock`.
- `_events: list[dict]`, `_event_index: dict[str,int]` (event_id → position),
  `_emitted_count: int` (how many broadcast).
- `_superseded_pending: dict[str,dict]` — already-emitted events a later read updated in
  place, re-broadcast so the client upgrades its held copy (copied verbatim from codex —
  streaming text/tool-status updates ARE supersessions of an already-shown event).
- `_updated_cursor: int` — the `time_updated` watermark (ms). The lowest `time_updated` NOT
  yet known-settled; re-scan from here every poll.
- `_root_session_id: str | None`, `_session_ids: frozenset[str]` — resolved from the marker;
  refreshed each poll (a subagent session can appear mid-turn).
- `_path_watcher: PathWatcher | None`.

### Tailing contract (the crux — DB rows mutate in place)

opencode has no integer `idx` like antigravity; its ordering key is `(time_created, id)` and
its **change** key is `time_updated`. So the cursor is a `time_updated` watermark, and dedup
is by **stable event_id + content supersession** (codex's `_ingest_event`), which absorbs the
in-place updates:

1. Resolve `_session_ids` (root + descendants, Q3) from the marker.
2. `read_changed_messages(db, _session_ids, since_updated=_updated_cursor)`:
   ```sql
   -- messages touched since the cursor, OR owning a part touched since the cursor
   SELECT m.id, m.session_id, m.time_created, m.time_updated, m.data
   FROM message m
   WHERE m.session_id IN (:sessions)
     AND (m.time_updated >= :cursor
          OR EXISTS (SELECT 1 FROM part p
                     WHERE p.message_id = m.id AND p.time_updated >= :cursor))
   ORDER BY m.time_created, m.id;
   -- then, for those message ids: SELECT * FROM part WHERE message_id IN (...) ORDER BY id
   ```
   Use `>=` (not `>`) so same-millisecond updates are never skipped; content supersession
   makes the resulting re-parse of an unchanged row a no-op (dropped as an identical dupe).
3. For each changed message: build its events (§4) and `_ingest_event` each — append a new
   `event_id`, **supersede in place + re-broadcast** when an existing id's content changed,
   drop an identical duplicate. This is codex's method, unchanged.
4. **Advance the cursor** only past *settled* messages, to bound re-reads to the live turn:
   a message is **settled** when `role=="user"`, OR (`role=="assistant"` AND `finish`/
   `time.completed` present AND every `tool` part `state.status` ∈ {`completed`,`error`}).
   Set `_updated_cursor = 1 + max(time_updated over the contiguous leading run of settled
   messages)`; a still-streaming message and everything after it stays hot and is re-read next
   poll (antigravity's "advance only through the leading terminal run", expressed on
   `time_updated`). A conservative-but-correct fallback is to never advance past the single
   oldest unsettled message's `time_updated`.

Because event ids are opencode's own (`msg_`/`prt_`), a `mngr stop`/`start` (server restart,
in-memory maps reset) re-reads the db from `cursor=0` on first poll and dedups every prior
event against nothing-yet-emitted → the accumulated transcript rematerialises without dups.

### Watching + lifecycle
- `start()`: prime the backlog WITHOUT broadcasting (read once under lock so existing history
  populates `_events`; the REST tail path delivers it — the prime-vs-poll split codex/claude
  use), then `PathWatcher.build((<db parent dir>,), self._emit_unsent).start()`. Watch the
  **directory** `plugin/opencode/data/opencode/` (recursively), NOT the `.db` file: WAL-mode
  writes land in `opencode.db-wal`, so the main file's mtime may not move until checkpoint —
  watching the dir catches the `-wal` appends. 1s poll is the safety net (`PathWatcher`
  already provides both). Idempotent.
- `stop()`: `_path_watcher.stop()`. Idempotent.
- `_emit_unsent()`: refresh (incremental read), then broadcast `_superseded_pending` +
  `_events[_emitted_count:]`; advance `_emitted_count`. Byte-for-byte the codex method.
- `_refresh()` runs at the top of every read method too (a read must never depend on the poll
  loop having run — codex/antigravity both do this), so the first request after a restart
  reflects the on-disk db rather than answering "no history".

### Read API
Identical to codex's (`get_all_events`, `get_tail_events`, `get_backfill_events`,
`get_forward_events`, `get_events_at_offset`, `get_event_offset`, `get_total_event_count`) —
all serve from `_events`/`_event_index` under `_lock` after `_refresh()`. `session_id` arg is
inert (single logical session to the UI). `get_subagent_metadata` → `None`,
`is_main_session_event` → `True` for v1 (Q3). Queue methods: inherit the base no-ops (Q5).

---

## 4. `[NEW] harnesses/opencode/session_parser.py` — rows → common events

Reproduces `mngr_opencode_plugin.ts buildCommonRecords()` in Python (same event_ids, same
fields), so the DB-tail and `mngr transcript` renderings agree. `SOURCE =
"opencode/db_transcript"` (distinct from the plugin's `opencode/common_transcript`, so the
origin is legible; the frontend keys on `type`, not `source`).

`build_message_events(message: OpenCodeMessage, parts: list[OpenCodePart]) -> list[dict]`:

- **user** (`role=="user"`): join `text` parts' `text` (skip `synthetic`); if empty, emit
  nothing. Else one:
  ```python
  { "timestamp": iso(message.time_created), "type":"user_message",
    "event_id": f"{message.id}-user", "source": SOURCE, "role":"user",
    "content": text, "conversation_id": message.session_id, "message_uuid": message.id }
  ```
- **assistant** (`role=="assistant"`): one `assistant_message`, then a `tool_result` per
  terminal tool part.
  ```python
  tool_calls = [ _tool_call(part) for part in parts if part.kind=="tool" ]
  { "timestamp": iso(message.time_created), "type":"assistant_message",
    "event_id": f"{message.id}-assistant", "source": SOURCE, "role":"assistant",
    "model": f"{provider_id}/{model_id}" if both else None,
    "text": joined text parts (skip synthetic),
    "tool_calls": tool_calls,
    "stop_reason": message.finish,          # opencode's finish reason ("stop", ...)
    "usage": None,                          # Q6: tokens live on message.data.tokens
    "message_uuid": message.id }
  ```
  Each `tool_call` (labelled here, where the harness is known — the events.py contract):
  ```python
  input_preview = json.dumps(state_input, separators=(",",":"))[:200 unless kept]  # MAX_TOOL_INPUT_PREVIEW_LENGTH
  header_label, caption_label = tool_labels(tool_name, input_preview)
  { "tool_call_id": part.call_id, "tool_name": part.tool_name,
    "input_preview": input_preview, "header_label": header_label, "caption_label": caption_label }
  ```
  Then, for each tool part with `state_status ∈ {completed, error}` (a running tool emits NO
  result yet — the two-phase emission that keeps the activity caption on "tool running"):
  ```python
  output = state_error if status=="error" else state_output
  { "timestamp": iso(message.time_created), "type":"tool_result",
    "event_id": f"{part.id}-tool_result", "source": SOURCE,
    "tool_call_id": part.call_id, "tool_name": part.tool_name,
    "output": truncate(output, 2000),       # MAX_TOOL_OUTPUT_LENGTH
    "is_error": status=="error", "message_uuid": part.id }
  ```

Timestamp helper: opencode stores ms epoch integers; render ISO-8601 `...Z` (mirror the
plugin's `_isoFromMs`) so it sorts/compares against the other harnesses' string timestamps.

tk-lifecycle preservation: opencode runs `tk` via the `bash` tool, and the chat progress view
reads `tk create --step`/`tk close` decoration out of tool input/output. Reuse the shared
`tk_command_parsing.parse_command` gate (as claude/codex parsers do) to exempt a `bash` call
whose command is a `tk create|start|close` from the 200-char `input_preview` truncation, and
keep tk decoration lines past the 2000-char output cut (port `_truncate_tool_output`). Without
this, a batched step plan or long close summary is clipped and the timeline breaks.

---

## 5. `[NEW] harnesses/opencode/tool_labels.py` — verbs / titles / captions

The per-harness label table (the "verbs, titles, captions" table pi/claude/codex each have).
`tool_labels(tool_name, input_preview) -> (header_label, caption_label)`, same signature as
the claude peer. opencode reports the real tool id on every `tool` part (like claude, unlike
codex's `exec`), so the header is `Tool: <Title>` and only the caption's verb+target is
derived. Reuse the shared helpers (`basename`, `shorten`, `quoted`, `first_string_value`,
`mcp_caption`, `parse_input_preview`, `GENERIC_CAPTION`) from `harnesses/tool_labels.py`.

**Vocabulary is opencode's own** — extracted from the installed TUI's caption functions in
the binary (so our captions read like opencode's, and header nouns match claude's where a tool
is equivalent, so harnesses read alike). opencode tool ids (verified from the binary's
title-map keys): `bash, edit, write, read, grep, glob, list, patch, webfetch, websearch,
task, skill, todowrite, todoread, question, invalid` (+ MCP tools, Q7).

| opencode tool | input key(s) for target | header (`Tool: …`) | caption verb | caption target |
|---|---|---|---|---|
| `bash` | `description` then `command` | `Bash` | Running | shortened description/command |
| `edit` | `filePath` | `Edit` | Editing | `basename` |
| `write` | `filePath` | `Write` | Writing | `basename` |
| `read` | `filePath` | `Read` | Reading | `basename` |
| `grep` | `pattern` | `Grep` | Searching | `quoted(pattern)` |
| `glob` | `pattern` | `Glob` | Searching | `quoted(pattern)` |
| `list` | `path` | `List` | Listing | `basename` |
| `patch` | `filePath`/`files` | `Edit` | Editing | `basename` or `…` |
| `webfetch` | `url` | `WebFetch` | Fetching page | `shorten(url)` |
| `websearch` | `query` | `WebSearch` | Searching the web | `quoted(query)` |
| `task` | (subagent) | `Task` | — | `Delegating to sub-agent…` |
| `skill` | `name` | `Skill` | Loading skill | `shorten(name)` |
| `todowrite` | — | `TodoWrite` | Updating todos | — (`Updating todos…`) |
| `todoread` | — | `TodoRead` | Reading todos | — |
| `question` | — | `Question` | Asking | `a question` |
| `invalid` / unknown | — | `Tool: <id>` | — | `GENERIC_CAPTION` |

`bash` uses `description` before `command` (claude parity — the agent's own description reads
better than a clipped shell line). `task` captions as a delegation, not a verb+target (claude's
`_SUBAGENT_CAPTION` pattern). Anything not in the table → `Tool: <id>` + `GENERIC_CAPTION`
(the honest fallback, names the tool). Build the header noun from a `{tool_id: Title}` map so
an unknown id still yields `Tool: <id>` rather than a crash.

`[NEW] harnesses/opencode/icon.svg` — already exists (used by the model catalog); reused.

---

## 6. `[EDIT] harnesses/registry.py` — swap the placeholder for the real watcher

```python
from imbue.system_interface.harnesses.opencode.watcher import OpenCodeDbSessionWatcher
...
HarnessType.OPENCODE: HarnessSpec(
    name=HarnessType.OPENCODE,
    watcher_class=OpenCodeDbSessionWatcher,     # was OpenCodePlaceholderSessionWatcher
    tracker_class=OpenCodeActivityTracker,      # unchanged (see §7)
    resolver_class=OpenCodeModelResolver,       # unchanged
    catalog_factory=get_opencode_catalog,       # unchanged
    special_kinds=frozenset(),                  # no turn markers emitted (see §7)
),
```

Delete `OpenCodePlaceholderSessionWatcher` (or keep briefly behind a flag while validating).
`harness_type.py` already has `OPENCODE = "opencode"` — no change.

---

## 7. `[EDIT?] harnesses/opencode/activity.py` — likely no change

`OpenCodeActivityTracker` already derives claude-style (lifecycle `active` marker + transcript
tail via `has_unmatched_tool_use`/`last_event_type`). It was inert only because the placeholder
watcher emitted nothing. With the real watcher emitting matched `tool_call`/`tool_result`
pairs and message events, that derivation becomes live **with no code change** — a running
tool is an unmatched `tool_call` (→ working), a settled one matches (→ thinking/idle), and the
`opencode_ready`/`active` markers gate the lifecycle. Keep `special_kinds=frozenset()`.

Optional upgrade (Q2): opencode has a true turn boundary (`session.status` busy/idle →
`active` marker), so a codex-style turn latch is *possible*, but it needs the plugin to emit
`turn_started`/`turn_completed` `special` events (more plugin work). Recommend the claude-style
heuristic for v1 — zero new plugin surface, and it already exists.

---

## 8. Tests — mirror antigravity's

- `[NEW] harnesses/opencode/testing.py` — a fixture builder that writes a real
  `opencode.db` (create `message`/`part`/`session` tables per §1, insert rows, plus an
  `update_part_state`/`settle_message` helper that rewrites a row in place + bumps
  `time_updated`, modelling a streaming turn). This is the analogue of antigravity's
  `testing.py`/`build_steps_db`. **No live opencode needed.**
- `[NEW] db_reader_test.py` — JSON `data` parsing for each part/message shape; malformed
  `data` skipped; descendant-session resolution.
- `[NEW] session_parser_test.py` — user/assistant/tool_result mapping; event_id stability;
  two-phase (running tool → no result; settled → result); tk-decoration preservation;
  parity of a fixture against the plugin's `buildCommonRecords` output.
- `[NEW] tool_labels_test.py` — every tool id → expected `(header, caption)`; unknown id and
  MCP fallbacks; bash description-over-command.
- `[NEW] watcher_test.py` — the tailing contract: incremental read advances only past settled
  messages; a streaming part update supersedes its event (not a duplicate); restart re-reads
  from 0 without dups; WAL-lock (`sqlite3.Error`) is skipped, not raised; read methods refresh
  from disk without the poll loop. Model these on `antigravity/watcher_test.py` and
  `codex/watcher_test.py`.
- `[EDIT] registry` test — `test_every_harness_has_a_spec` already covers the wiring; add an
  opencode watcher smoke test if antigravity has one.

---

## 9. Build order

1. `db_reader.py` + `testing.py` fixture (+ tests) — the DB read layer, no watcher yet.
2. `session_parser.py` + `tool_labels.py` (+ tests) — pure mapping, parity-checked against
   `buildCommonRecords`.
3. `watcher.py` (+ tests) — the tailing loop (cursor/settle/supersede), reusing `PathWatcher`.
4. `registry.py` swap; delete the placeholder. Manually verify against a live opencode agent
   (create one, send a turn with a bash tool call, watch the tab stream), then confirm
   activity flips working↔idle.
5. Changelog entry under `system/apps/system_interface/changelog/`.

---

## Open questions (carry into build)

- **Q1 — cursor settle vs simplicity.** The `time_updated` watermark + "advance past leading
  settled run" bounds re-reads to the live turn. Is the conservative fallback (never advance
  past the oldest unsettled message) enough, or is the leading-run tracking worth it for a
  very long, mostly-settled transcript? (Antigravity does the leading-run version.)
- **Q2 — activity model.** Claude-style heuristic (recommended, zero plugin work) vs a
  codex-style turn latch fed by new plugin `special` events.
- **Q3 — subagents.** v1 shows the **root session only** (`is_main_session_event =
  event.session_id == root`, filter `_session_ids` to `{root}`). opencode's task tool spawns
  child sessions (`parent_id` set) — surface them via `get_subagent_metadata` + descendant
  filtering later, matching claude's subagent linkage. Decide whether v1 filters to root or
  includes descendants inline (the plugin's common transcript includes ALL sessions flatly).
- **Q4 — reasoning parts.** opencode emits `reasoning` parts (thinking). The plugin drops
  them; antigravity surfaces them as an optional `thinking` key on `assistant_message`.
  Recommend v1 parity (drop), add `thinking` as an enhancement.
- **Q5 — queued messages.** opencode has a `session_input` table (queue: `prompt`,
  `delivery`, `admitted_seq`, `promoted_seq`) that could power the queued-message snapshot
  like codex's sidecar. v1 inherits the base no-op; wire later if the shoulder-tap surface is
  wanted for opencode.
- **Q6 — usage/tokens.** `message.data.tokens` and `session` token columns exist; emit
  `usage` on `assistant_message` (input/output/cache) if the model-bar/cost UI wants it.
- **Q7 — MCP tool naming.** opencode exposes MCP tools as `<server>_<tool>` (single
  underscore), NOT claude/codex's `mcp__<server>__<tool>`, so the shared `mcp_caption` won't
  match. Decide: a small opencode-specific MCP recognizer, or let unknown ids fall to
  `Tool: <id>` + generic caption (acceptable v1).
- **Q8 — schema drift.** opencode self-upgrades; the `data` JSON shapes and even columns can
  move (`opencode_config.py` already documents `project_directory` drift). The
  `session`/`message`/`part` + JSON-`data` + `ses_`/`msg_`/`prt_` shape is stable across
  targeted versions, but the reader must degrade (skip a row it can't parse) rather than
  crash, and the fixture should be re-confirmed when `OPENCODE_VERSION` moves.
```
