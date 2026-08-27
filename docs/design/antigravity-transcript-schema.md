# antigravity's transcript schema, as measured

agy stores each conversation as a protobuf SQLite `.db` (`steps` table) with no published
schema. This file records what the wire format actually contains, measured against **agy
1.1.20** on two live conversation stores (73 and 31 steps; 41 tool calls between them).

It exists because the decoder used to *guess* -- it kept the longest printable run it could
find anywhere in a step -- and that guess returned the tool's **arguments** instead of its
output for 41 of 41 tool steps. No `tk` line ever reached the chat, so the step progress view
never drew a node for the entire life of the harness. Guessing is what this document replaces.

## Step payload

```
step payload
  [1]   step_type   varint
  [4]   status      varint
  [5]   metadata    CortexStepMetadata
  [19]  user input      (USER_INPUT steps)
  [20]  planner response (PLANNER_RESPONSE steps)
  [24]  error message   (ERROR_MESSAGE steps)
  [140] body        the tool call's own record (tool steps)
```

### [5] metadata — CortexStepMetadata

```
  [1]  created_at   google.protobuf.Timestamp { [1] seconds, [2] nanos }
  [3]  source       varint  (2 MODEL, 3 USER_IMPLICIT, 4 USER_EXPLICIT, 5 SYSTEM, 6 SYSTEM_SDK)
  [4]  tool_call    ChatToolCall { [1] call id, [2] name, [3] args (JSON string) }
  [30] caption_short   DECLARED BUT ABSENT -- 0 of 41 rows
  [31] caption_long    DECLARED BUT ABSENT -- 0 of 41 rows
```

The captions are **not** here despite the field numbers existing. They live in the body.

### [140] body

```
  [1]* repeated { [1] key, [2] value }   the call's arguments, plus agy's own captions
  [2]  result container
       [2.1] result TEXT                 the command's output
       [2.6] echoed nested Step          a copy of the step, carrying the captions again
```

The repeated `[1]` pairs for a `run_command` are `CommandLine`, `Cwd`, `WaitMsBeforeAsync`,
plus `toolSummary` and `toolAction`.

`140 -> 2 -> 1` resolves the result for **41 of 41** tool steps, zero misses -- including an
errored command (`tk ls`, exit 1) and one that printed nothing (`tk ready`).

Result text always carries agy's own preamble:

```
\nThe command exited with code 0.\nOutput:\n<stdout>
```

(sometimes `Stdout:`/`Stderr:` instead of `Output:`). It is passed through as-is: the exit code
is real information, and the shape varies enough that stripping it is a parsing job of its own.

## Step types

Measured distribution across both stores: `{132: 41, 15: 51, 14: 10, 23: 2}`.

| value | meaning | note |
|---|---|---|
| 14 | `USER_INPUT` | text at `[19]`; only `source=4` (USER_EXPLICIT) is a real user message |
| 15 | `PLANNER_RESPONSE` | assistant text + thinking at `[20]` |
| 23 | `SESSION_IDENTITY` | one per conversation, SYSTEM source, no user-visible content |
| 132 | `TOOL_CALL` | **every** tool call, whatever the tool |
| 5, 7, 8, 9, 21, 91 | older per-tool types | never observed; agy consolidated on 132 |

Dispatch keys off the decoded tool call rather than the type name, so an unrecognised type still
parses; the names are for diagnostics.

## Captions

Every tool call carries two model-authored strings in the body's argument pairs:

| key | shape | examples |
|---|---|---|
| `toolSummary` | 2-5 word noun phrase | `Task tracking`, `Test execution` |
| `toolAction` | 2-5 word verb phrase | `Creating step`, `Running test call 1 of 20` |

Present on **41 of 41** calls. `toolAction` is the better caption source than anything
synthesised from a truncated command echo, but promoting it to primary inverts a documented
decision (agy currently synthesises labels from the vocabulary shared with claude and codex, so
the harnesses read alike) and is therefore a separate change. It is wired as the fallback today.

Not verified: whether either is present while a step is still RUNNING. Every captured row is
settled -- agy overwrites a step row in place as it progresses, so a RUNNING snapshot did not
survive in either store.

## agy's declared tools

Seventeen, self-reported by the agent when asked for its schemas. Argument names are
case-sensitive as given.

| tool | key arguments |
|---|---|
| `run_command` | `CommandLine`, `Cwd`, `WaitMsBeforeAsync`, `RunPersistent?`, `RequestedTerminalID?` |
| `view_file` | `AbsolutePath`, `StartLine?`, `EndLine?`, `ContentOffset?` |
| `write_to_file` | `TargetFile`, `CodeContent`, `Overwrite`, `Description`, `ArtifactMetadata?` |
| `replace_file_content` | `TargetFile`, `StartLine`, `EndLine`, `TargetContent`, `ReplacementContent`, `AllowMultiple`, `Instruction`, `Description` |
| `list_dir` | `DirectoryPath` |
| `find_by_name` | `SearchDirectory`, `Pattern`, `Type?`, `Extensions?`, `Excludes?`, `FullPath?`, `MaxDepth?` |
| `grep_search` | `SearchPath`, `Query`, `IsRegex?`, `CaseInsensitive?`, `MatchPerLine?`, `Includes?` |
| `manage_task` | `Action` (list/kill/status/send_input), `TaskId?`, `Input?` |
| `schedule` | `Prompt`, `DurationSeconds?`, `CronExpression?`, `TimerCondition?`, `MaxIterations?` |
| `invoke_subagent` | `Subagents[]` of `TypeName`, `Role`, `Prompt`, `Model?`, `Workspace?` |
| `define_subagent` | `name`, `description`, `system_prompt`, `enable_*_tools?` |
| `manage_subagents` | `Action` (list/kill/kill_all), `ConversationIds?` |
| `send_message` | `Recipient`, `Message` |
| `search_web` | `query`, `domain?` |
| `read_url_content` | `Url` |
| `generate_image` | `Prompt`, `ImageName`, `AspectRatio?`, `ImagePaths?` |
| `ask_question` | `questions[]` of `question`, `options`, `is_multi_select?` |

Every call additionally carries `toolSummary` and `toolAction`.

Two consequences worth noting elsewhere:

- `Cwd` is **required per call**, so agy has no persistent shell cwd. Claude's "return to the
  repo root" Stop hook has nothing to protect here -- see
  `harnesses/core-contracts/tool-call-policies-state-of-things.md`.
- `WaitMsBeforeAsync` is **agent-supplied**, not fixed by the runtime (5000 in one store, 3000
  in the other), and `manage_task` exists to drive backgrounded shells.

`multi_replace_file_content` is absent from this list but is still mapped in `tool_labels.py`,
where it aliases harmlessly to `Edit`. One agent's self-report is not proof a tool was removed,
and dropping the mapping would silently degrade the caption if it reappears.

## How to re-measure

The stores live inside the agent container at
`<agent_state_dir>/plugin/antigravity/home/.gemini/antigravity-cli/conversations/<id>.db`, with
the ids listed in `<agent_state_dir>/antigravity_conversation_ids`. Copy one out with
`sqlite3 <db> ".backup /tmp/agy.db"` (never read the live file -- it has a WAL), then walk
`SELECT idx, step_type, status, step_payload FROM steps`.

When agy changes shape, the failure to expect is a **silent** one: fields move, a lookup returns
empty, and the chat quietly loses a feature. Add a test against a captured payload rather than a
synthetic one -- synthetic payloads built by our own helpers are exactly what let the original
bug pass CI.
