# Grok Build's transcript schema, as measured

Grok Build appends each session's log to `updates.jsonl`, with no published schema. This file
records what the log contains, measured against **grok 1.0.62** with a stub model: mngr's TUI
captures and the chat app's own TUI and headless captures, all under
`system/apps/chat/imbue/chat/harnesses/grok/fixtures/`. The chat app's parser
(`harnesses/grok/session_parser.py`) reads exactly these shapes.

## Where the log lives

`$GROK_HOME/sessions/<bucket>/<session id>/updates.jsonl`, appended and flushed one line at a time.
An mngr agent's home is `<agent state dir>/plugin/grok/home`. A subagent runs as a child session
with its own directory, named by its parent's `subagent_spawned` record.

## Records

```
{"timestamp": <epoch seconds>,
 "method": "session/update" | "_x.ai/session/update",
 "params": {"sessionId": "...",
            "update": {"sessionUpdate": <kind>, ...},
            "_meta": {"eventId": "<session id>-<n>", "agentTimestampMs": <epoch ms>}}}
```

- `eventId`'s counter restarts with every launch, and the stub's `toolCallId`s repeat within a
  session (`call_stub_1` three times in `foreground_subagent_failing_hooks`), so the parser's ids
  come from the record's byte offset.
- Grok writes compact JSON with non-ASCII characters as they are: re-serialising a record with
  `json.dumps(record, ensure_ascii=False, separators=(",", ":"))` gave back 117 of 117 lines byte
  for byte.
- `output` and `stdout` fields are sometimes a JSON list of byte values rather than a string.

| Kind | Fields the chat reads | Chat event |
|---|---|---|
| `user_message_chunk` | `content.text`; `_meta.promptIndex`, `modelId`, `hideFromScrollback`, `syntheticPrompt` | a user turn; a synthetic one is a notice |
| `agent_message_chunk` | `content.text` | assistant text |
| `agent_thought_chunk` | `content.text` | `has_thinking` on the next assistant event |
| `tool_call` | `toolCallId`, `rawInput`, `_meta["x.ai/tool"].name` and `.label` | a tool call |
| `tool_call_update` | `status` (`null`, `completed`, `failed`), `content`, `rawOutput` | the call's result, once `completed` or `failed` |
| `turn_completed` | `prompt_id`, `stop_reason` (`end_turn`, `cancelled`, `interrupted`, `error`), `agent_result` | the turn's end |
| `subagent_spawned` | `child_session_id`, `description`, `subagent_type` | links the child session to its `spawn_subagent` call |
| `task_completed`, `subagent_finished` | `task_snapshot.task_id`, `subagent_id`, `will_wake` | background work reported over |
| `hook_annotation` | `message` | a Stop hook's continuation |
| `compaction_checkpoint` | none | a top-level session's compaction, as the "Context was compacted" status; a child session's is dropped |

A user turn that reads `The user sent a message while you were working:` followed by a
`<user_query>...</user_query>` block is grok's send-now wrapper, and the chat shows the words inside
it, as mngr's transcript converter does; no capture here holds one.

Ignored: `hook_execution`, `retry_state`, `task_backgrounded`, `background_tasks`, `plan`,
`available_commands_update`, `session_info_update`, the other compaction kinds and the rewind kinds.

## Assistant text

One run of text is one `agent_message_chunk` line, written when the run ends: 600 deltas
streamed over 33 s gave one line (its `eventId` jumped from 78 to 678), and 30,000 deltas (210 KB)
gave one line. A background subagent that ends while the run streams splits it: grok writes
`subagent_finished`, then the text so far and the rest as two consecutive lines (in both of two
runs; `tui_sessions/subagent_ends_mid_reply` is one). The chat joins consecutive text lines into
one message. Text sent before a tool call in the same response is its own line ahead of the
`tool_call`. `reasoning_content` deltas become one `agent_thought_chunk` line ahead of the text.

## Tool results

A call gets a status-less `tool_call_update` first (a shell call's content is its description, an
edit's is a diff, every other tool's is empty), then one `completed` or `failed` update. A
foreground tool killed by a cancel gets no terminal update. The text the model was given, read
back from the stub's request log, is in `rawOutput` for some tools and only in the content blocks
for others:

| `rawOutput.type` | Text the model got | Content blocks |
|---|---|---|
| `Bash` | `output_for_prompt`: `exit: <code>`, then the output | the output |
| `GrepSearch` | `stdout` | `found <n> matches` |
| `ListDir` | `Content.content`; a failure as `IsAFile` | none |
| `ReadFile` | `FileContent.content`; a failure as `FileNotFound` | the same text |
| `SearchReplace` | `EditsApplied.tool_output_for_prompt`; `NoMatchesFound.message` | a diff, or the message |
| `TaskOutput` | a formatted report; the output is `Result.output` | none |
| `KillTask` | `killed: ` and `Result.message` | none |
| `BackgroundTaskStarted` | a `<task-id>` block, not in the record | `Background task <id> started` |
| `Monitor`, `SchedulerList` | a message that is not in the record | none |
| `Text`, `SubagentCompleted` | the content text | the content text |
| (none: a `use_tool` failure) | `message` | the same text |

A long shell output keeps only its first and last 19.6 KB, in the content and in
`output_for_prompt`, whose first line becomes `exit: 0 [truncated: showing first/last 19.6 KB of
2.6 MB - full output at: <path>]` for a 2.6 MB output; a request object echoed at the end survives.

### Exit codes

| Call | `status` | `exit_code` | Model text starts |
|---|---|---|---|
| `exit 3` | `completed` | 3 | `exit: 3` |
| `false` (stderr only) | `completed` | 1 | `exit: 1` |
| `grep -q` with no match | `completed` | 1 | `exit: 1` |
| killed by SIGTERM | `failed` | -1 (`signal: "signal 15"`) | `exit: killed (signal 15)` |
| refused by a `PreToolUse` hook | `failed` | | `Hook denied: <the hook's message>` |
| `read_file` of a missing file | `failed` | | `Error: ... does not exist.` |
| `search_replace` with no match | `failed` | | `The string to replace was not found ...` |
| `list_dir` of a file | `failed` | | `Error: ... is a file, not a directory.` |

Grok marks every shell command that ran to its end `completed`, whatever its exit code; the chat
reads a non-zero shell exit as a failed call, as it does claude's `Bash`.

A call a `PreToolUse` hook refused (`headless_sessions/guard_refusals`, the workspace's own policy
hook) gets a `hook_execution` run with `status: failed`, a `hook_annotation{kind: "tool_outcome",
message: "`<tool>` blocked by <hook>: <message>"}`, and one `failed` update whose only content is
`Hook denied: <message>`, with no `rawOutput`; grok shortens a long message with `… [+<n> chars]`.
The chat shows such a call as failed with no snippet, as it does claude's hook errors.

## Turns

`turn_completed` closes every root turn, including a cancelled one (no `usage` when no model call
finished) and a failed one: a wake turn whose replies stayed empty wrote 15 `retry_state` records
over 327 s, then `turn_completed{stop_reason: "error", agent_result: "empty response from model
(no_visible_content)"}`. A child session's turns end the same way inside its own log.

A grok that exits mid-turn (measured with Ctrl+Q twice during a tool) writes nothing for that
turn, and its killed tool gets no terminal update. When `--resume` next loads the session, grok
closes the turn with `turn_completed{stop_reason: "interrupted", agent_result: "Grok stopped
before this turn finished (the agent process exited or was restarted). Committed tool results were
kept."}`, time stamped at the resume (`tui_sessions/resume_after_quit`).

## Background work

`task_completed` (a shell or a monitor) and `subagent_finished` carry `will_wake`: whether grok
starts a turn of the session for it once the session is idle. It is `false` for a task the model
read with a blocking `get_command_or_subagent_output`, one it killed, a shell started by a turn
that was then cancelled, and a subagent stopped with its turn; a subagent left running by a
cancelled turn still wakes the session. A background subagent that ends during a later turn wakes
the session after that turn ends (`tui_sessions/subagent_ends_mid_reply`).

## Wake prompts

When background work it started reports in, grok writes a `user_message_chunk` with
`_meta.hideFromScrollback` and `syntheticPrompt` true, as its own turn. Measured shapes, each
wrapped in `<system-reminder>` unless noted:

| `turn_completed.prompt_id` | Text |
|---|---|
| `task-completed-<task id>` | `Background task "<id>" completed (exit code: 0).` / `Description: <d> \| Duration: 8.0s` / a retrieval hint |
| `task-completed-<monitor id>` | `Monitor "<id>" ended: [monitor ended: exited (code 0)].` / `Description: <d> \| Duration: 1.0s` |
| `subagent-completed-<child id>` | `While you were idle, 1 background subagent completed:` / `- [<type>] "<d>" — completed successfully (16.4s, 0 tool calls)` / the child's report |
| `notifications-<id>` | no reminder: `<monitor-event task_id="<monitor id>">` / `[<d>] <line>` / `</monitor-event>` |

The binary names more prompt kinds (`parent-agent-message-`, `parent-message-`,
`workflow-completed-`, `goal-summary-`, `plan-resume-`, `bash-completed-`, `monitor-completed-`),
not measured; the chat shows any synthetic prompt it does not recognise as a generic notice.

## Hook annotations

A Stop hook that blocks writes `hook_annotation{message: "↩ Stop blocked by <hook>, continuing:
<feedback>"}`, and the reply that follows stays in the same turn. The binary also carries
`Stop hook feedback, continuing: ` and `Stop hooks kept the agent working <n> times this turn:
limit reached, ending the turn`, not measured.

## Tools

A session's `tool_definitions.json` lists 24 or 25 tools (the headless captures add
`send_feedback`): `run_terminal_command`, `monitor`, `read_file`, `write`, `search_replace`,
`grep`, `list_dir`, `spawn_subagent`, `get_command_or_subagent_output`,
`kill_command_or_subagent`, `todo_write`, `scheduler_create`, `scheduler_list`,
`scheduler_delete`, `search_tool`, `use_tool`, `workflow`, `enter_plan_mode`, `exit_plan_mode`,
`ask_user_question`, `image_gen`, `image_edit`, `image_to_video`, `reference_to_video`.
`wait_commands_or_subagents`, `web_fetch` and `web_search` are named in the binary but were not
offered. `use_tool` runs only MCP integration tools: given `run_terminal_command` it fails with
"`run_terminal_command` is a native tool, not an MCP integration tool".

## Not yet captured

A real latchkey gateway's echo and the real secret-request script (the `stamps` capture echoes
their shapes with `printf`), the rule blocks of a trusted folder's `AGENTS.md`, a real account's
`web_fetch` and `web_search`, a prompt written as more than one `user_message_chunk`, a child
session's `interrupted` turn, and a background shell that ends during a turn.

## How to re-measure

1. Use the pinned binary, and never launch it without a stub model configured: with no credential
   it starts the device login, which opens a browser.
2. Give each run its own `GROK_HOME` and `HOME`. Its `config.toml` points the default model at the
   stub (`[models] default = "stub"`, `[model.stub]` with `base_url = "http://127.0.0.1:<port>/v1"`
   and any `api_key`), sets `[ui] permission_mode = "always-approve"` and turns off telemetry and
   auto-update; `trusted_folders.toml` trusts the work dir.
3. The stub serves `GET /v1/models` and a streaming `POST /v1/chat/completions` that replays a
   script of tool calls and text (`reasoning_content` deltas for thoughts), and logs every request
   body: the tool messages in the next request are the text the model got.
4. Drive grok in its TUI on a private tmux server (mngr's `sessions/`, the chat app's
   `tui_sessions/`) or headless with `grok -p "<prompt>" --output-format streaming-json`
   (`headless_sessions/`).
5. Copy `<GROK_HOME>/sessions/<bucket>/<session id>/updates.jsonl` into a scenario directory and
   write its `roots` file.
6. Rewrite the capture paths in strings and in byte-value lists, then re-serialise each record as
   above so every other byte is unchanged.

When grok changes shape, expect a silent failure: a field moves, a lookup comes back empty, and
the chat loses a card or a result without an error. Test against a new capture, never against a
record built by hand.
