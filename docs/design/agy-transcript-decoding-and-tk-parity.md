# agy transcript decoding, and one place per harness for tk

Supersedes `tk-cleanup-plan.md`, which accumulated too many corrections to trust. Every number
here was re-verified in a single pass against two live agy conversation stores.

## The point, in one paragraph

Two kinds of harness-specific knowledge are in the wrong place. agy's transcript decoder
**guesses** at a binary format instead of reading it, and it guesses wrong 100% of the time --
which is why agy's step timeline has never drawn a node. Separately, the rule for recognising a
`tk` command is **copy-pasted into all four harnesses**, and two of them leak their tool names
into the transcript parser while one leaks into the frontend. The fix for the first is to read
the format. The fix for the second is to let each harness answer exactly one question -- *where
do you keep the shell command?* -- and answer everything else once, centrally.

## Part A -- the bug

### What is broken

agy's step timeline has **never rendered a single node**. Not degraded; zero, since the harness
shipped.

### Why

`agy_transcript._tool_result_text` does not parse the tool result. It walks the protobuf and
keeps the longest printable run it can find. For agy, that is the **arguments**, not the output:

```
tk create --step "Run sequential test commands"
  what the frontend receives today:
    {"CommandLine":"tk create --step \"Run sequential test commands\"",
     "Cwd":"/tmp/mngr_antigravity_workspaces/agent-792f8...",
     "WaitMsBeforeAsync":5000,"toolAction":"Creating step","toolSummary":"Task tracking"}
```

Measured across both stores: **41 tool results, 41 wrong** -- 39 return the arguments JSON, 2
return a mangled substring of the real output. So no `Created`, no `Updated`, no `tk-step` line
has ever reached the frontend.

The mechanism is only that the args JSON (~180 chars) is longer than a tk output (~94). Patching
the incidental `0x0a`-is-a-tag-byte misread in `_looks_like_text` and re-running leaves
**41/41 still wrong**. Do not record that misread as the cause; it explains nothing.

### Why that kills the timeline specifically

`tk` writes state to `data/.tickets/`, but **the frontend never reads it and cannot** -- it is
browser JS against the chat API with no view of that filesystem. The only channel from tk to the
chat is what tk *prints*:

```
Created af-step-ob7z: Build the notes app
Updated af-step-ob7z -> in_progress
tk-step af-step-ob7z title: Build the notes app
tk-step af-step-ob7z summary: Shipped capture + search
```

That text rides the tool result into the transcript, and `turn-grouping.ts` regexes those four
shapes out of it. tk is an **announcement**, not shared state. Corrupt the tool result and the
timeline loses its only input.

This is not only a tk bug: **every** agy tool result is the arguments, so the real output of
every command an agy agent has ever run has been invisible in chat.

### Nothing else is at fault

Checked individually, on real data: `display="hidden"` is stamped correctly on every pure-tk
call; hidden calls still carry their results (`parseMessage` looks up by `tool_call_id`
regardless of display); agy's one-`assistant_message`-per-call with `text=""` is handled;
sections anchor on field-19 `USER_EXPLICIT` messages; `isStepId` accepts agy's id shape; `\r\n`
does not break the JS regexes; regular-ticket closes correctly mint no phantom nodes.

### The real schema

Measured against agy 1.1.20:

```
step payload
  [1]   step_type          132 for tool steps
  [4]   status
  [5]   metadata -> [5.4] ChatToolCall: .1 call id, .2 tool name, .3 args JSON
  [140] body
        [1]*  repeated {key, value}: CommandLine, Cwd, WaitMsBeforeAsync,
              toolSummary, toolAction
        [2]   result container
              [2.1]  result TEXT      <- the output
              [2.6]  echoed nested Step
```

`140 -> 2 -> 1` resolves **41/41**, zero misses, including an errored command (`tk ls`, exit 1)
and an empty-output one (`tk ready`).

Two incidental corrections the same measurement produced:

- Live tool steps are type **132**. Our table maps `21: RUN_COMMAND`; 21 never occurs (nor
  5/7/8/9/91 -- agy consolidated tool steps). Type **23** occurs once per conversation (session
  identity, no user content) and currently renders as `STEP_TYPE_23`.
- `DecodedStep.caption_short` / `caption_long` read metadata `f30`/`f31`, which are present on
  **0 of 41** rows. Two permanently-`None` fields feeding a fallback that can never fire.

## Part B -- Ship 1: read the format

`harnesses/antigravity/agy_transcript.py`. This is the entire user-visible fix.

```python
# The step body: agy's own record of the call it ran. Field numbers measured against agy
# 1.1.20 on live conversation stores -- see antigravity-transcript-schema.md.
_STEP_BODY: Final[int] = 140
_BODY_ARG_PAIR: Final[int] = 1     # repeated {key, value}: args + agy's own captions
_BODY_RESULT: Final[int] = 2
_ARG_KEY: Final[int] = 1
_ARG_VALUE: Final[int] = 2
_RESULT_TEXT: Final[int] = 1


def _iter_messages(blob: bytes, field_number: int) -> Iterator[bytes]:
    """Every length-delimited value on ``field_number``: the repeated counterpart to
    :func:`_first_message`. New helper -- nothing needed a repeated-field reader until the
    body's argument pairs.

    The ``_WIRE_LEN`` test is load-bearing. ``_iter_fields`` also yields ``bytes`` for the
    fixed-width wire types (1 and 5), so an ``isinstance`` check alone would hand an 8-byte
    double to ``_first_str`` as though it were a message.
    """
    for field, wire, value in _iter_fields(blob):
        if field == field_number and wire == _WIRE_LEN and isinstance(value, (bytes, bytearray)):
            yield bytes(value)


def _body_args(payload: bytes) -> dict[str, str]:
    """The call's argument pairs: CommandLine, Cwd, and agy's own toolSummary/toolAction."""
    body = _first_message(payload, _STEP_BODY)
    if body is None:
        return {}
    args: dict[str, str] = {}
    for pair in _iter_messages(body, _BODY_ARG_PAIR):
        key, value = _first_str(pair, _ARG_KEY), _first_str(pair, _ARG_VALUE)
        if key and value:
            args[key] = value
    return args


def _tool_result_text(payload: bytes) -> str:
    """The command's output, read from the body's result field.

    NOT a search. The previous implementation kept the longest printable field it could find,
    which returned the ARGUMENTS for every agy tool call ever recorded -- so no tk line ever
    reached the chat and the step timeline never drew a node.

    Returns "" and never None. The caller emits a ``tool_result`` event only when this is not
    None, so returning None for a body shape we do not recognise would leave that call
    permanently unmatched and pin the activity indicator at TOOL_RUNNING for the life of the
    agent -- worse than the bug being fixed. Only ``run_command`` bodies are verified (all 41
    observed steps are ``run_command``); the other 16 tools are unmeasured, so the unrecognised
    path must stay harmless.
    """
    try:
        body = _first_message(payload, _STEP_BODY)
        result = _first_message(body, _BODY_RESULT) if body is not None else None
    except TruncatedError:
        return ""
    if result is None:
        return ""
    return _first_str(result, _RESULT_TEXT)[:_MAX_RESULT_CHARS]
```

**Deleted:** `_longest_printable`, `_looks_like_text`. `grep` confirms this module holds the only
copy in the repo; nothing else uses them, and `user_text` / `assistant_text` / `thinking` /
`error_text` already read explicit field numbers.

**Three details that are each a regression if dropped:**

| detail | what breaks without it |
|---|---|
| returns `str`, never `None` | first unmeasured tool shape hangs on "running" forever |
| keeps `[:_MAX_RESULT_CHARS]` | 4000-char cap goes dead; unbounded string reaches `find_permission_request` |
| keeps `except TruncatedError` | `watcher.py` stops scanning that conversation permanently on one corrupt row |

`DecodedStep` gains `tool_summary` / `tool_action` from `_body_args`, and drops the dead
`caption_short` / `caption_long`. Fix `_STEP_TYPE_NAMES` (132, and map 23).

### Proven, not asserted

An independent review monkeypatched this in, ran the real backend parser, and fed the events
through the **real** `buildSections` from `turn-grouping.ts` under vitest:

```
agy   before: 6 sections, 0 step nodes
      after:  3 nodes  title="Run sequential test commands"
                       summary="Executed 20 sequential test commands."
                       active -> active(carryover) -> done
agy3  before: 0 nodes  ->  after: 1 node, "Execute 20 test commands"
```

## Part C -- Ship 2: one place per harness for tk

Independent of Part B. Nothing here is user-blocking.

### The two rules

Both already live in `harnesses/tool_output.py` and neither should be reimplemented per harness.

- **Hide rule** (`is_pure_tk_lifecycle_command`, strict): the command must *start* with the tk
  verb. `cd /code && tk start x` does not hide, because it also does real work, and silently
  swallowing real work is the failure being guarded against.
- **Truncation-exemption rule** (broad, segment-wise): fires if a tk verb appears anywhere, so a
  batched plan or long close summary survives display truncation. Over-preserving is harmless;
  over-hiding is not.

The asymmetry is deliberate. State it once instead of four times.

### What is duplicated today

| | copies |
|---|---|
| `_TK_LIFECYCLE_VERBS = {"create","start","close"}` | 4 |
| `from tk_command_parsing.parser import parse_command` | 4 |
| `any(segment.tk_verb in _TK_LIFECYCLE_VERBS ...)` | 4 |

| harness | `tool_labels` | `keeps_full_tool_input` | command accessor |
|---|---|---|---|
| claude | yes | **missing** -- private `_is_tk_lifecycle_call` in the parser | inline `== "Bash"` |
| codex | yes | yes | `is_tk_lifecycle` -> **bool** |
| pi | yes | yes | inline `== "bash"` |
| antigravity | yes | yes | `run_command_line` -> **str \| None** |

### The shared half

```python
# tool_output.py, beside the hide rule
def is_tk_lifecycle_anywhere(command: str) -> bool:
    """Truncation-exemption rule: a tk lifecycle verb ANYWHERE in the command.

    Deliberately broader than :func:`is_pure_tk_lifecycle_command`. Uses the shared shlex
    parser, so a `tk close` merely mentioned inside a quoted argument is not mistaken for a
    real call.
    """
    parsed = parse_command(command)
    return parsed is not None and any(s.tk_verb in _TK_LIFECYCLE_VERBS for s in parsed.segments)
```

### The per-harness half

Each `tool_labels.py` answers one question and never what tk is:

```python
# antigravity  (rename of run_command_line)
def shell_command(tool_name: str, args_json: str) -> str | None:
    if tool_name != "run_command":
        return None
    return _first_string_ci(parse_input_preview(args_json), ("CommandLine",))

# claude  (moved out of session_parser.py)
def shell_command(tool_name: str, raw_input: str) -> str | None:
    if tool_name != "Bash":
        return None
    command = parse_input_preview(raw_input).get("command")
    return command if isinstance(command, str) else None

# pi_coding  (moved out of session_parser.py; same body, different tool name)
def shell_command(tool_name: str, raw_input: str) -> str | None:
    if tool_name != _BASH_TOOL_NAME:
        return None
    command = parse_input_preview(raw_input).get("command")
    return command if isinstance(command, str) else None

# codex  (replaces is_tk_lifecycle)
def shell_command(tool_name: str, raw_input: str) -> str | None:
    """codex runs the shell from inside code mode, so the command is an argument of an
    ``exec_command`` call in emitted JS rather than a tool input of its own."""
    if tool_name != CODE_MODE_TOOL_NAME:
        return None
    call_match = _CODE_MODE_CALL_RE.search(raw_input)
    if call_match is None or call_match.group(1) != "exec_command":
        return None
    return _js_string_argument(raw_input, "cmd")
```

**On the name.** `tk_lifecycle_command(...)` would bake the tk rule into every harness and let
four copies drift -- the exact problem being fixed. `shell_command` asks the only genuinely
harness-specific question.

### What the call sites become

All four `session_parser.py`, identical:

```python
command = shell_command(tool_name, raw_input)
is_pure_tk = command is not None and is_pure_tk_lifecycle_command(command)
display = classify_tool_call_display(is_pure_tk=is_pure_tk, raw_input=raw_input)
if display is not None:
    tool_call["display"] = display.value
```

The **tk clause** of `keeps_full_tool_input` becomes identical everywhere:

```python
command = shell_command(tool_name, raw_input)
return command is not None and is_tk_lifecycle_anywhere(command)
```

### Where the harnesses are genuinely not symmetric

Do not paper over these; the template applies to the tk clause only.

- **codex must keep its own first clause verbatim.** Its file-body exemption keys off the inner
  JS function, not `tool_name`, with two apply-patch cases -- including one where the body was
  front-loaded into a variable and there is no visible call for `shell_command` to find. A
  `frozenset[str]` of tool names expresses neither. Written from the template, codex loses its
  patch exemption and every diff is cut at 200 chars.
- **`_KEEPS_FULL_BODY_TOOLS` exists only in antigravity.** claude and pi exempt no file bodies at
  all. That is a real latent difference (a claude `Write` of a large file is truncated where an
  agy `write_to_file` is not) -- flagged, not fixed here.
- **claude has no public `keeps_full_tool_input`.** It gains one, behaviour unchanged (tk only),
  replacing the private `_is_tk_lifecycle_call` its parser calls inline.

### Net effect

`_TK_LIFECYCLE_VERBS` 4 -> 1. `parse_command` import 4 -> 1. Segment walk 4 -> 1. codex's
bool-shaped outlier and claude's misplaced private helper both deleted. Tool names in
`session_parser.py`: 2 harnesses -> 0. Every `tool_labels.py` then exposes the same three
functions, and a fifth harness needs only `shell_command`.

## Part D -- Ship 3: the render layer

`turn-grouping.ts` has no harness branch except one line inside `tkCommand`:

```ts
if (tc.tool_name !== "Bash" && tc.tool_name !== "bash") return null;
```

**Delete that line, not the function.** The decoration regexes are already tk-anchored and
`TK_CREATE_OR_CLOSE_RAW` is the cheap pre-filter, so removing the gate deletes the harness
knowledge *and* makes the input fallback work for agy for the first time. One line, versus ~35
to remove the fallback entirely.

If the fallback is ever removed instead: old transcripts lose titles **and close summaries**
(`applyInputFallback` sets both), and **two** test blocks depend on it --
`turn-grouping.test.ts:353` and `:1037`, the latter framed as a window-scroll case whose
`summary === "did it"` assertion would need deliberate re-baselining.

## Part E -- deferred, with reasons

- **`toolAction` captions.** Every call carries a model-authored verb phrase (`toolAction`,
  41/41) and noun phrase (`toolSummary`). `'Running test call 1 of 20'` beats our synthesised
  `'Running python3 -c "import time; time.sle...'`. But `antigravity/tool_labels.py` explicitly
  promises the opposite ("we synthesize the labels from the shared vocabulary... agy's own f30
  caption is the graceful fallback"), so this inverts a documented contract and deserves its own
  change. Also unverified mid-execution: every captured row is settled.
- **The result preamble.** Correct results carry agy's own `\nThe command exited with code 0.
  \nOutput:\n` (sometimes `Stdout:/Stderr:`). It does not affect the regexes. Stripping means
  parsing a varying format and discarding the exit code, which is real information the other
  harnesses do not surface. Revisit as a display question once the timeline is live.
- **Tool coverage.** agy declares 17 tools; we label 10. Missing ones need header nouns only.
  Do **not** drop `multi_replace_file_content` -- it aliases harmlessly to `Edit`, and removing
  it because one agent's self-report omitted it would silently degrade the caption if agy
  re-adds it.
- **The cwd Stop hook.** claude's `[ -e .git ] || "return to the repo root"` exists because
  claude's `Bash` carries cwd between calls. agy's `Cwd` and codex's `workdir` are per-call and
  mandatory -- across 41 live calls, always present, only ever the agent's own root, zero `cd`
  invocations. It is claude-only in the repo today (one occurrence) and should stay that way.
  Record the reason in `tool-call-policies-state-of-things.md`, along with the correction that
  `WaitMsBeforeAsync` is agent-supplied (5000 in one store, 3000 in the other), not fixed.

## Part F -- tests

Existing agy tests build payloads with `build_step_payload(...)`, whose synthetic bodies never
start with a newline. That is exactly why a 100%-failure bug passed CI. **Use captured real
payloads**, committed as fixtures.

Ship 1:
1. A real `tk create` row decodes to the tk output, not the arguments. Fails today.
2. Parsing a real conversation yields a `tool_result` containing `Created <id>: <title>` -- the
   end-to-end guard for the timeline.
3. An unrecognised body still **emits a `tool_result` event**. Assert the event, not the return
   value: the failure being pinned is a stuck activity indicator, invisible in a decoder-only
   test.
4. `TruncatedError` mid-decode yields `""` rather than propagating.
5. `_iter_messages` skips wire types 1 and 5.
6. A real row's `tool_action` is `"Creating step"`.

Ship 2:
7. `shell_command` per harness: returns the command for that harness's shell tool, `None`
   otherwise. One parametrised body.
8. `is_tk_lifecycle_anywhere("cd x && tk start s1")` is True while
   `is_pure_tk_lifecycle_command` on the same string is False. Pins the asymmetry.
9. codex `keeps_full_tool_input` still True for **both** apply-patch cases.

Ship 3:
10. Decoration resolves for an agy-shaped (`run_command`) tool call, which today's gate rejects.

## Part G -- order

**Ship 1 alone, first.** ~6 lines of production change; it is the entire user-visible fix and
the timeline has been dead since the harness shipped. Ships 2, 3 and Part E are separable,
independently revertible, and none of them are needed for the timeline.
