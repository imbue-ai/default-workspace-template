# Tool-call policies: the state of things

Which harness currently enforces which policy in `tool-call-policies.md`, how each one is
wired, and what the delivery channel actually permits. The policies beside this file are
timeless; everything here is a snapshot.

**If you wire something, update the table. If a channel changes, update the contract table.**

## At a glance

| Policy | claude | codex | pi | agy | grok |
|---|---|---|---|---|---|
| P1 no pipe into `tail`/`head` | live | live | live | live | live |
| P2 no git history rewrite | live | live | live | live | live |
| P3 permission request stands alone | live | **partial** | live | **partial** | live |
| P4 OOM band + git identity | live | live | live | live | live |
| P5 substantive work under a step | live | **partial** | live | **partial** | live |
| P6 `tk start`/`close` stands alone | live | **partial** | live | live | live |
| P7 open steps are reconciled | live | live | live | **live (turn-start only)** | **live (first tool call)** |
| P8 a chat turn with something for the user notifies | prose-only | prose-only | prose-only | prose-only | prose-only |
| P9 secret file read only by the wrapper | live | live | live | **partial** | live |
| P10 secret request stands alone | live | **partial** | live | **partial** | live |

No harness is fully `n/a` any more, and five rows are `partial` for reasons that are
structural rather than unwired. Measured against codex-cli 0.147.0, pi 0.84.1 and grok 1.0.62.

- **grok P7 — live, first tool call.** grok discards an allowing UserPromptSubmit hook's output
  (JSON `additionalContext` and plain stdout alike, measured), so the prompt only records the
  turn and the reminder rides the first allowed PreToolUse of that turn. A turn that makes no
  allowed tool call gets none.
- **grok P9 — live, partly by grok's own permission rule.** The shell half is live, and so is
  every grok tool that names a file: `read_file`, `search_replace`, `write` and `grep`'s `path`
  are refused under the directory, and `list_dir` lists it as `ls` does. Two routes reach a
  secret file without naming it, so no check on the call's text sees them: `grep` with a `glob`
  such as `*`, and `use_tool` reading its arguments from a file (both under "Known bypasses").
  The `[agent_types.grok]` settings carry `[permission] deny = ["Read(**/.secrets/**)"]`, which
  closes both. That rule also denies `read_file` the directory's README, which `cat` still reads.

- **agy P5 — partial.** The shim sees only `bash -c`, so agy's own editing tools
  (`write_to_file`, `replace_file_content`) can never be nudged: no agy hook carries tool
  identity and neither tool spawns a process. The SHELL half is wired (the check judges the
  command, not the tool name). Previously recorded as `n/a` on the grounds that the skip list
  keys on claude tool names -- only half true, since the script has always had a command-shaped
  branch.
- **agy P7 — live, turn-start only.** The reminder rides the shim's stderr, which becomes the
  tool result the model reads, keyed on the `active` marker's inode (which changes once per
  turn). Previously recorded as `n/a` because "agy has no prompt-submit event and its stop
  stderr goes to a tmux pane nobody reads" -- both true, and neither was the relevant channel.
- **codex P3 / P6 — partial.** Both policies require the guarded thing to be the only thing in
  its TOOL CALL. Under code mode a tool call is a JS program that may hold several
  `tools.exec_command(...)` calls; measured, one `custom_tool_call` produced three PreToolUse
  events with three unrelated `tool_use_id`s and no field naming the outer call, so no guard can
  count per call. Each inner call is still checked on its own. The display layer no longer acts
  on the unverifiable claim (see `codex/session_parser.py`).
- **codex P5 — partial.** Its read-only skip list is keyed on claude tool names, none of which
  codex ever sends (measured: `Bash`, `apply_patch`, `update_plan`). The command-shaped
  read-only allowlist now covers the shell half; `apply_patch` is correctly nudge-worthy.
- **agy P9 -- partial.** The shell half is live through the shim; the file-tool half is not,
  for the same reason as P5: agy's own file tools never reach a shell and no agy hook carries
  tool identity.
- **codex and agy P10 -- partial**, exactly as P3: the same checker, the same per-call counting
  limit under codex code mode, and the same shim path on agy.
- **P7's stop half is decorative on EVERY harness, claude included.**
  `agent_open_tickets_stop_nudge.sh` says so itself ("mainly for orchestrator log / human
  visibility") and exits 0 unconditionally; on codex a sentinel written at Stop appears in no
  transcript item. The half that reaches the model is the turn-start reminder.
- **P8 is prose-only on every harness, claude included.** A Stop hook reaches the model only
  by exiting 2, and claude delivers that by refusing the stop and injecting the hook's text as a
  user message, which the chat renders as a "Stop hook feedback" chip followed by a second
  assistant reply -- on every turn, since the ask was unconditional (found in the minds-v0.7.0
  staging rehearsal, 2026-09-22). `stop_hook_active` keeps that from looping but cannot hide the
  extra turn, so the claude hook was removed rather than wired on codex. pi's `agent_settled`
  and agy's `Stop` give stderr and nothing else, so they never could carry it. The rule lives in
  `AGENTS.md` and the `notify-user` skill, which every harness reads.

## Delivery channels, per harness

What each harness's mechanism can actually do. A policy can only be wired where its required
capability exists.

| | block | rewrite | tell the agent something |
|---|---|---|---|
| claude | `exit 2` + stderr | `updatedInput` | `additionalContext` |
| codex | `exit 2` + stderr | `updatedInput` **with** `permissionDecision: "allow"` | `additionalContext` |
| pi | `{block, reason}` | mutate `event.input.command` | append to `tool_result` content |
| agy | `exit 2` + stderr (becomes the tool result) | prepend before `exec` | stderr on the same result |
| grok | `exit 2` + JSON `permissionDecisionReason` (stderr alone reaches the model as its first line only) | `updatedInput` alone; one that fails the tool's schema denies the call | PreToolUse `additionalContext` (UserPromptSubmit output is discarded) |

## How each harness attaches

### claude  (wiring: `.claude/settings.json`)
Runs the `.sh`/`.py` scripts in this directory, one entry per hook under the matching event
(`PreToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`). A script reads the event JSON on
stdin. This is the reference implementation and needs no changes.

### codex  (wiring: `.codex/hooks.json` in this repo)
Codex speaks the **same hook protocol** as claude — same event names, same stdin payload
(`tool_name`, `.tool_input.command`, claude-shaped even under code mode), same output
channels — so codex **reuses the exact same scripts**, referenced from the work dir
(`$MNGR_AGENT_WORK_DIR/system/scripts/…`). No copy, no new logic: editing a script updates
claude and codex at once.

Codex loads hooks from every active config layer and a higher-precedence layer does not
replace a lower one, so this repo's `.codex/hooks.json` runs alongside the per-agent file
mngr writes for its own bookkeeping hook. Adding a guard is an edit to
`.claude/settings.json` and `.codex/hooks.json` (and `.pi/extensions/policy_guards.ts`
below) — never a mngr release.

A project layer's hooks need the layer trusted and each hook trusted by hash; mngr already
marks the work dir trusted and passes `--dangerously-bypass-hook-trust`, which covers both.

**Re-testing codex by hand: reproduce mngr's trust setup or you will measure nothing.** codex
refuses to run command hooks unless they are trusted, so a probe without it sees *zero* hook
events and looks exactly like "the policy is not wired". Production is covered three ways:
`--dangerously-bypass-hook-trust --enable hooks` on the launch command (pinned by
`mngr_codex/plugin_test.py`), `merge_project_trust` seeding `[projects."<path>"] trust_level =
"trusted"`, and `auto_dismiss_dialogs = true` for the TUI prompt. Pass the flag and seed the
project entry when reproducing.

### agy  (wiring: `system/scripts/agy_shim/bash` + one PATH entry from the plugin)
agy has **no usable hook surface**. Measured on 1.1.20: it declares `PreToolUse`/`PostToolUse`
but never fires them; the events that do fire (`SessionStart`, `PreInvocation`,
`PostInvocation`, `Stop`) carry no tool identity -- no `tool_name`, no `tool_input`, no
command -- and no hook output channel reaches the model (plain stdout and the binary's own
`systemMessage` key were both tested; the model could not see the injected marker). The
payload is conversation-scoped only: `conversationId`, `invocationNum`, `modelName`,
`transcriptPath`, `workspacePaths`, `artifactDirectoryPath`, plus `error`, `executionNum`,
`fullyIdle` and `terminationReason` on `Stop`.

It does, however, run every shell tool call as `bash -c "<CommandLine>"`, resolving `bash`
from `PATH`. So the guards run from a **shim named `bash`**, early on the agent's PATH, which
gets all three capabilities the hooks were for: block (exit 2; stderr becomes the tool
result), rewrite (prepend, then `exec`), and inform.

The shim feeds the **same scripts claude runs**, unmodified, by synthesising the payload they
parse on stdin (`{"tool_name":"Bash","tool_input":{"command":…}}`) -- so editing a guard
updates claude, codex, agy and pi together. It runs them in claude's order on the command the
agent wrote, then applies the rewrite prefix, so no guard ever inspects a prefixed command.

Three things about it are load-bearing:
* **The shebang is `#!/bin/bash`, absolute.** Every other script here uses
  `#!/usr/bin/env bash`; in a file that IS `bash` on PATH that is a fork bomb.
* **The payload is built with `jq -Rs`, never interpolation.** A command containing a quote
  would otherwise produce invalid JSON, exit non-zero under the guards' `set -e`, and be
  passed through by fail-open -- letting the agent defeat every guard with a trailing `# "`.
* **It fails OPEN.** It is on the path of every command; a dead shim bricks the agent. Only an
  explicit exit 2 blocks. This is a seatbelt, not a boundary -- the agent could `rm` it.

Only the outermost `bash -c` is guarded (`MNGR_AGY_BASH_SHIM` marks the environment), so a
build's own nested shell is not policed. `MNGR_AGY_SHIM_OFF=1` disables it without a redeploy.

**#5 is n/a on agy, not merely unwired.** Its skip list is keyed on claude TOOL NAMES; under
the shim every call is `Bash`, so it would nudge agy's read-only shell work while never seeing
agy's own edit tool -- wrong in both directions. That discipline lives in `AGENTS.md`.
**Both halves of P7 are n/a**: agy has no `UserPromptSubmit`, and its stderr goes to a tmux
pane nobody reads -- the same conclusion pi reached for the stop half.
**#3 is live, with a correction to an earlier claim.** This section previously said agy "sets
`WaitMsBeforeAsync` on every `run_command` and runs the child synchronously, so there is no
agent-controllable background flag". That is wrong. `WaitMsBeforeAsync` is a **required
agent-supplied parameter** -- "milliseconds to wait before detaching to background" -- and agy
also declares `manage_task` for driving backgrounded shells. Two live stores show two different
values (5000 and 3000), i.e. the model chooses per call. The guard is still live, because the
shim sits on the command before any of that; but the `--backgrounded` arm is not provably
unreachable, and an agent that wants to background a command can.

**The cwd Stop hook is claude-only, by construction -- do not port it.** claude's
`.claude/settings.json` carries `[ -e .git ] || "Be sure to return to the repo root..."`. It
exists because claude's `Bash` tool keeps one shell cwd ACROSS calls, so an agent that wanders
breaks every later hook that uses a relative path. agy and codex cannot hit that: agy's `Cwd`
and codex's `workdir` are required per-call parameters. Measured across 41 live agy calls, `Cwd`
was present every time, only ever the agent's own workspace root, with zero `cd` invocations.
The hook has nothing to protect there, and it appears exactly once in this repo -- it was never
given to codex or pi either. This paragraph records why, so it is not "fixed" later.

### pi  (wiring: `.pi/extensions/policy_guards.ts` in this repo)
Pi has **no shell-hook surface** — its only extension point is a TypeScript module, which pi
auto-discovers from `.pi/extensions/` in the project. Two extensions here carry our rules:

* **The command guards and the rewrite** live in `.pi/extensions/policy_guards.ts`. Like the
  agy shim, it feeds the **same scripts claude runs** the claude-shaped payload they parse on
  stdin, in claude's order, and refuses the call (`{block, reason}`) on exit 2 with the
  script's stderr as the reason. Once every guard passes it prepends the output of
  `agent_rewrite_bash_command.py --prefix-only` to `input.command`. Any other guard or rewrite
  outcome fails open and leaves a line in `pi_policy_guards.log` in the agent's state dir, as
  the shim does in `agy_shim.log`. Editing a guard script updates pi along with claude, codex
  and agy.
* **The tk step discipline** lives in `.pi/extensions/tk_workflow.ts` — the require-steps
  nudge on `tool_result`, the open-steps carryover on `before_agent_start`, and the stop
  nudge on `agent_settled`. pi composes across extensions (`tool_result` handlers chain like
  middleware, `before_agent_start` chains the system prompt), so it runs alongside mngr's
  lifecycle extension without either clobbering the other. Reminder *text* is copied
  verbatim from the scripts so all harnesses read identically, and step state comes from the
  same vendored `ticket` binary they read.

mngr's own pi extension carries no command policy: pi's guards are this repo's, exactly as
claude's and codex's are.

**Which command a guard sees.** The "rewriter runs last so blockers see the original" story is
only true on **agy**, **pi** and **grok**, where the guards and the rewrite run in sequence in one
script (agy, and grok's dispatcher) or one `tool_call` handler (pi). It is belt-and-braces on claude and codex and
should not be relied on there: claude runs a matcher's hooks in PARALLEL
(`agent_rewrite_bash_command.py` says so in its own comment), and codex does not thread
`updatedInput` into later hooks of the same event (measured: a rewriting hook placed first, a
logging hook second, and the logger saw the original). On pi, keep the rewrite inside
`policy_guards.ts`'s handler: pi calls every extension's `tool_call` handler on one shared,
mutable event, so a rewrite in another extension could land before the guards read the
command, and the prefix (its own `;`-joined commands) would make the tk and permission-request
checkers refuse every `tk start`/`tk close` and every filing as chained.

The SDK is documented in the package's `dist/core/extensions/types.d.ts`; we do NOT modify it.

**Which pi runs which extension.** This repo's two extensions are auto-discovered from
`.pi/extensions/` (it is one of the cwd trust inputs pi asks about), so any pi that runs *here*
and trusts the project — managed or not — is held to the guards and the step discipline.
mngr's lifecycle extension, which carries no policy, is loaded only via the `-e` flag mngr adds
to a managed agent's launch command (`plugin.py::assemble_command`).

### grok  (wiring: `.grok/hooks/policy.json` + `system/scripts/agent_grok_policy_guards.py`)
grok speaks claude's hook protocol (its payload carries claude's snake-case keys beside its own
camelCase ones, and it reads `hookSpecificOutput` replies), but names each tool by its own name:
a shell call is `run_terminal_command`, not `Bash`. So one PreToolUse hook, the dispatcher,
translates every call and runs the **same scripts claude runs**, unmodified, in claude's order
(`PRETOOLUSE_SCRIPTS`, held to `.claude/settings.json` by `agent_hook_wiring_test.py`), the
rewrite after every guard. grok runs a project's hooks in sequence and blocks the turn on each,
so one process with a prefilter per guard is the cheaper shape: measured, a median of 104 ms per
allowed shell call and 90 ms per `read_file`, against 167 and 163 ms for claude's seven hooks
wired directly and 23 ms for a no-op `python3` hook; most of the dispatcher's time is the
require-a-step check.

* **Shell calls** (`run_terminal_command`, and `monitor`, whose command also runs a shell and
  whose result is always a background task) reach the guards as
  `{"tool_name":"Bash","tool_input":{"command","run_in_background"}}`. The background flag is
  grok's own rule, measured: when `block_until_ms` is set it decides (`0` backgrounds), and when
  it is absent or `null`, `is_background: true` or `timeout: 0` backgrounds; claude's
  `run_in_background` means nothing to grok.
* **Every other tool** reaches only the guards that police every tool (`agent_secrets_guard.sh`)
  and the step nudge, under its grok name and input.
* **`use_tool`** arrives under the name of the tool it calls, with `{tool_name, tool_input}` as
  its input (grok reads a `tool_input_file` or `file` before the hook runs), and is judged as
  that call. grok refuses its own tools through `use_tool`, so in practice the call inside is an
  MCP tool's.
* **Refused outright:** `workflow`, and a shell call whose `toolInputTruncated` is true (grok
  cuts a tool input past 128 KiB and the cut hides the end of the command). A truncated file
  tool is still judged, on its path, which grok never cuts.
* **Replies.** A refusal exits 2 with the guard's reason on stderr and as the JSON
  `permissionDecisionReason`, since grok shows the model only the first stderr line of a plain
  exit 2. A rewrite is `updatedInput` with the tool's other fields kept; it needs no
  `permissionDecision`. grok's `updates.jsonl` records the original input on the `tool_call` and
  the rewritten one on every `tool_call_update`.
* **Fail open.** Only a guard's exit 2 refuses. A crash, a missing script or a run past the
  hook's budget (`PRE_TOOL_USE_BUDGET_SECONDS`, under the hook's `timeout`) is logged to
  `grok_policy_guards.log` in the agent's state dir and the call goes ahead; so is anything the
  dispatcher itself did not expect. grok fails a handler open when its `timeout` runs out (5 s
  for PreToolUse when unset) and never loads one whose `timeout` is negative or not a number, so
  every handler in `policy.json` sets one, in seconds.
* **P7.** UserPromptSubmit records the turn (`grok_policy_turn.json`: `sessionId`, `promptId`) for
  a root session's prompt; a subagent's carries `subagentType` and records nothing. The first
  allowed PreToolUse with that `sessionId` claims the record by renaming it and runs
  `agent_open_tickets_reminder.sh`; a refused call leaves it for the next one.

Project hooks load only for a trusted folder, which mngr seeds for every work dir. Measured:
`.grok/hooks/policy.json` runs for a root session in a linked worktree whose main checkout is the
trusted one, and in every kind of subagent (shared, `cwd`, `isolation: "worktree"`). The `Stop`
entries are the stop nudge and code-guardian's stop gate, by its absolute path under
`/opt/agent-plugins` (grok sets no plugin root), gated by `.reviewer` settings as on claude.
There is no cwd `Stop` hook: grok runs hooks from the workspace root.

## Output contracts (the reference the mapping below relies on)

**codex** (verified against codex-cli 0.146.0 — its manual, the generated hook output schemas
in `openai/codex` under `codex-rs/hooks/schema/generated/`, and live runs):

| Need | Channel codex honors |
|------|----------------------|
| Block a tool call | `exit 2` + reason on stderr (or `hookSpecificOutput.permissionDecision: "deny"`) |
| Rewrite a tool call | `permissionDecision: "allow"` **and** `updatedInput` together — `updatedInput` alone is rejected |
| Soft reminder on a tool | PreToolUse `hookSpecificOutput.additionalContext` (exit 0) — added as developer context |
| Reminder on a new prompt | UserPromptSubmit — **plain stdout is added as developer context** (also accepts `additionalContext`) |
| Stop | plain stdout is **invalid**; `exit 2` / `decision: "block"` **continues** the agent (creates a new prompt), it does **not** hold the stop |

**pi** (from `types.d.ts`, the installed compiled runtime, and the public `earendil-works/pi`
source — all in agreement):

| Need | Channel |
|------|---------|
| Block a tool call | `on("tool_call")` → return `{block: true, reason}` |
| Rewrite a tool call | `on("tool_call")` → mutate `event.input.command` in place |
| Soft reminder on a tool | `on("tool_result")` → append text to the returned `content` (the model reads result content) |
| Reminder on a new prompt | `on("before_agent_start")` → return `{systemPrompt: base + reminder}` (guaranteed model-visible) |
| Stop | `on("agent_settled")` → the true "run fully settled" signal; stderr only |
| Read tk state | shell out to the vendored `ticket` script synchronously |

## Known bypasses

A guard is a seatbelt on an agent already trusted to run commands, not a sandbox boundary. These
are the routes around them that are worth knowing; all were measured.

- **codex `write_stdin` (closed).** The unified-exec surface adds `write_stdin` and a tty option,
  and `write_stdin` fires NO PreToolUse event -- so an agent could open a shell with one guarded
  call and type anything into it. Closed by `features.unified_exec_tty = false` in
  `.mngr/settings.toml`: every exec'd process runs with its stdin closed, so `write_stdin` can
  only poll a running command or interrupt it. `features.unified_exec = false` does NOT close it:
  codex >= 0.154 forces unified exec back on unless a managed requirements file pins it. Nor does
  the `tools` table: `ToolsToml` accepts only `web_search` / `experimental_request_user_input` /
  `update_plan`, and any other key there is silently ignored.
- **agy `manage_task` `send_input` (open).** Stdin piped into a task that a guarded
  `run_command` launched is not itself a tool call, so nothing sees it. No event exists to hook.
- **agy nested shells (open, deliberate).** Only the outermost `bash -c` is guarded; policing the
  whole tree would block third-party build scripts and re-apply the rewrite per level.
- **pi in an untrusted project (open).** `.pi/extensions/` is **trust-gated**: pi loads project
  extensions only `if (projectTrusted)`, and trust resolves to false for a non-interactive run
  with no stored decision -- so a nested `pi -p` started from a subdirectory gets NO guards at
  all. mngr seeds trust for the work dir, which covers the normal case.
- **grok in an untrusted folder (open).** `.grok/hooks/` loads only for a trusted folder, as pi's
  extensions do; mngr seeds trust for every work dir.
- **grok `use_tool` with `tool_input_file` or `file` (closed).** grok reads the file before
  PreToolUse, and the payload carries its parsed contents, not its path, so a JSON file under
  `data/.secrets/` (mcpc's `credentials.json` is one) reaches an MCP tool, and its result the
  model, without any guard seeing where it came from. A file that is not JSON (the `.env` a
  secret card writes) fails to parse, and nothing of it is shown. The dispatcher cannot refuse
  what it never sees; grok's own permission rule `Read(**/.secrets/**)` in `[permission] deny`,
  which the grok settings carry, refuses the read ("MCP source Read denied"), measured under
  always-approve.
- **grok `grep` with a match-all `glob` (closed).** ripgrep searches the hidden and ignored files
  a glob selects, so `glob: "*"` (which grok's own tool description suggests for searching
  ignored files), `**`, `?*` or `**/*` from the workspace prints the values in
  `data/.secrets/`, with no path naming the directory. A glob that does not select the
  directory itself (`*.env`, `**/*.env`, `data/.secrets/*`) finds nothing there. The same
  `Read(**/.secrets/**)` rule leaves those files out of grep's results while the search still
  runs, and refuses `read_file` there by an absolute path or a `..` spelling too, measured.
- **grok `workflow` and truncated inputs (closed).** The dispatcher refuses `workflow`, a script
  that runs many subagents as one background job, and any shell call grok truncated.
- **grok `!` commands (open, not agent-reachable).** By grok's own docs a `!` command typed into
  its composer fires no hook at all. The agent cannot type one; whether a chat message that
  starts with `!` becomes one is not measured.
- **`sh -c` / `eval` (open on every harness, including claude).** The blockers anchor on the
  command text, so re-entering through another interpreter evades them by construction.

## Keeping the harnesses in step

When a rule changes, update every harness that carries it:
- **Safety 1–4, 9, 10 and workflow 6** (`agent_prevent_commit_rewrite.sh`,
  `agent_block_pipe_tail_head.sh`, `agent_latchkey_request_standalone.sh`,
  `agent_secrets_guard.sh`, `agent_tk_standalone.sh`, `agent_rewrite_bash_command.py`): the
  scripts alone. claude and codex run them as hooks, and agy's shim, pi's
  `policy_guards.ts` and grok's dispatcher feed them the same payload. `agent_secrets_guard.sh`
  is the one guard pi and grok also run on their file tools, because P9 covers a `read` of a
  secret file as much as a `cat`; its checker lists each harness's file-tool names and path
  fields, so a new file tool is an entry there.
  Keep the rewrite **last** in both hook configs and in pi's handler — a blocker that
  inspects the rewritten command refuses everything (see "Which command a guard sees" above).
- **Workflow 5 and 7** (`agent_require_steps_pretool.sh`, `agent_open_tickets_reminder.sh`,
  `agent_open_tickets_stop_nudge.sh`): the scripts (claude, codex **and** grok) and the matching
  handler in **this repo's** `.pi/extensions/tk_workflow.ts` (pi). The step discipline is
  this repo's, not mngr's — mngr's lifecycle extension no longer carries any of it. The
  require-a-step script's read-only skip list carries claude's and grok's tool names; a new
  read-only tool on either is an entry there.
- **Workflow 8** (finish notification): no script on any harness. The rule is in `AGENTS.md`
  and the `notify-user` skill, which every harness reads (see P8 above for why the claude Stop
  hook was removed).
- codex, pi, agy and grok wiring lives in **this repo**: `.codex/hooks.json`,
  `.pi/extensions/policy_guards.ts`, `system/scripts/agy_shim/bash` and
  `system/scripts/agent_grok_policy_guards.py` (wired by `.grok/hooks/policy.json`). A guard
  added to `.claude/settings.json` needs the matching entry in all four, and nothing in mngr.
  For agy, pi and grok that entry is one line, because they run the claude script itself
  (`agent_hook_wiring_test.py` checks pi's list against claude's, and grok's list and order),
  plus, for pi and grok, a prefilter regex that matches everything the script can refuse. mngr's
  only contributions are agy's PATH entry and seeding pi's and grok's project trust.
- The tk reminders are the one place pi carries copies: they ride pi events no shell script
  can answer (`tool_result`, `before_agent_start`), so `tk_workflow.ts` restates their text
  verbatim.
