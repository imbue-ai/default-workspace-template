# Tool-call policies: the state of things

Which harness currently enforces which policy in `tool-call-policies.md`, how each one is
wired, and what the delivery channel actually permits. The policies beside this file are
timeless; everything here is a snapshot.

**If you wire something, update the table. If a channel changes, update the contract table.**

## At a glance

| Policy | claude | codex | pi | agy |
|---|---|---|---|---|
| P1 no pipe into `tail`/`head` | live | live | live | live |
| P2 no git history rewrite | live | live | live | live |
| P3 permission request stands alone | live | **partial** | live | **partial** |
| P4 OOM band + git identity | live | live | live | live |
| P5 substantive work under a step | live | **partial** | live | **partial** |
| P6 `tk start`/`close` stands alone | live | **partial** | live | live |
| P7 open steps are reconciled | live | live | live | **live (turn-start only)** |

No harness is fully `n/a` any more, and three rows are `partial` for reasons that are
structural rather than unwired. Measured against codex-cli 0.147.0 and pi 0.84.1.

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
- **P7's stop half is decorative on EVERY harness, claude included.**
  `agent_open_tickets_stop_nudge.sh` says so itself ("mainly for orchestrator log / human
  visibility") and exits 0 unconditionally; on codex a sentinel written at Stop appears in no
  transcript item. The half that reaches the model is the turn-start reminder.

## Delivery channels, per harness

What each harness's mechanism can actually do. A policy can only be wired where its required
capability exists.

| | block | rewrite | tell the agent something |
|---|---|---|---|
| claude | `exit 2` + stderr | `updatedInput` | `additionalContext` |
| codex | `exit 2` + stderr | `updatedInput` **with** `permissionDecision: "allow"` | `additionalContext` |
| pi | `{block, reason}` | mutate `event.input.command` | append to `tool_result` content |
| agy | `exit 2` + stderr (becomes the tool result) | prepend before `exec` | stderr on the same result |

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

### agy  (wiring: `system/scripts/agy_shim/bash` + one PATH entry from the plugin)
agy has **no usable hook surface**. Measured on 1.1.20: it declares `PreToolUse`/`PostToolUse`
but never fires them; the events that do fire (`SessionStart`, `PreInvocation`,
`PostInvocation`, `Stop`) carry no tool identity -- no `tool_name`, no `tool_input`, no
command -- and no hook output channel reaches the model (plain stdout and the binary's own
`systemMessage` key were both tested; the model could not see the injected marker).

It does, however, run every shell tool call as `bash -c "<CommandLine>"`, resolving `bash`
from `PATH`. So the guards run from a **shim named `bash`**, early on the agent's PATH, which
gets all three capabilities the hooks were for: block (exit 2; stderr becomes the tool
result), rewrite (prepend, then `exec`), and inform.

The shim feeds the **same scripts claude runs**, unmodified, by synthesising the payload they
parse on stdin (`{"tool_name":"Bash","tool_input":{"command":…}}`) -- so editing a guard
updates claude, codex and agy together. It runs them in claude's order on the command the
agent wrote, then applies the rewrite prefix, so no guard ever inspects a prefixed command;
pi needs `mngrOriginalCommand` for that property, the shim gets it from statement order.

Three things about it are load-bearing:
* **The shebang is `#!/bin/bash`, absolute.** Every other script here uses
  `#!/usr/bin/env bash`; in a file that IS `bash` on PATH that is a fork bomb.
* **The payload is built with `jq -Rs`, never interpolation.** A command containing a quote
  would otherwise produce invalid JSON, exit non-zero under the guards' `set -e`, and be
  passed through by fail-open -- letting the agent defeat every guard with a trailing `# "`.
* **It fails OPEN.** It is on the path of every command; a dead shim bricks the agent. Only an
  explicit exit 2 blocks. This is a seatbelt, not a boundary -- the agent could `rm` it.

Only the outermost `bash -c` is guarded (`MNGR_AGY_BASH_SHIM` marks the environment), so a
build's own nested shell is not policed -- the same "nested harness is unguarded" norm pi
already sets. `MNGR_AGY_SHIM_OFF=1` disables it without a redeploy.

**#5 is n/a on agy, not merely unwired.** Its skip list is keyed on claude TOOL NAMES; under
the shim every call is `Bash`, so it would nudge agy's read-only shell work while never seeing
agy's own edit tool -- wrong in both directions. That discipline lives in `AGENTS.md`.
**#7 and #8 are n/a**: agy has no `UserPromptSubmit`, and its stderr goes to a tmux pane
nobody reads -- the same conclusion pi reached for #8.
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
Pi has **no shell-hook surface** — its only extension point is a TypeScript module. It
therefore cannot run a hook *wrapper* (those read a hook payload on stdin, which pi has no
equivalent of), and splits our rules two ways:

* **This repo's guards** live in `.pi/extensions/policy_guards.ts`, which pi auto-discovers
  from the project. It spawns the same `*_check.py` files claude and codex reach through
  their wrappers, passing the agent's command as `$1` and blocking on exit 2 with the
  checker's stderr as the reason. pi calls every extension's `tool_call` handler and blocks
  when any returns `{block, reason}`, so this runs alongside mngr's lifecycle extension.
  One checker file, three harnesses.
* **This repo's tk step discipline** lives in `.pi/extensions/tk_workflow.ts` — the
  require-steps nudge on `tool_result`, the open-steps carryover on `before_agent_start`,
  and the stop nudge on `agent_settled`. pi composes across extensions (`tool_result`
  handlers chain like middleware, `before_agent_start` chains the system prompt), so it
  runs alongside mngr's without either clobbering the other. Reminder *text* is copied
  verbatim from the scripts so all three harnesses read identically, and step state comes
  from the same vendored `ticket` binary they read.
* **Rules that hold for any pi agent** (the pipe-into-`tail`/`head` block, the git
  history-rewrite block, and the OOM/git-identity rewrite) stay re-expressed in mngr's
  lifecycle extension against the pi SDK event that matches.

**Which command a guard sees.** The "rewriter runs last so blockers see the original" story is
only true on **agy**, where the shim runs the guards and the rewrite as statements in one
script. It is belt-and-braces everywhere else and should not be relied on: claude runs a
matcher's hooks in PARALLEL (`agent_rewrite_bash_command.py` says so in its own comment), and
codex does not thread `updatedInput` into later hooks of the same event (measured: a rewriting
hook placed first, a logging hook second, and the logger saw the original). On pi the order is
deterministic and is the UNSAFE one -- CLI `-e` extensions load before project ones -- which is
why mngr's lifecycle extension runs the blockers and the rewrite inside a single `tool_call`
handler, blockers first.
pi offers no such ordering: it calls every extension's `tool_call` handler on one shared,
mutable event, and mngr's rewrite prepends the OOM tag and git identity as their own
`;`-joined commands — which our checkers would refuse as "another command runs before it",
blocking every permission request and every `tk start`/`tk close`. So mngr's handler records
the pre-rewrite command on the event as **`mngrOriginalCommand`**, and
`policy_guards.ts` prefers it, falling back to `input.command` (the untouched value) when it
is absent because this extension ran first. Either order gives the guards the agent's own
command. Keep the two ends of that contract in step.

The SDK is documented in the package's `dist/core/extensions/types.d.ts`; we do NOT modify it.

**Which pi runs which extension.** mngr's lifecycle extension is loaded via the `-e` flag mngr
adds *only* to a managed agent's launch command (`plugin.py::assemble_command`), so a user
running plain `pi`, or a nested `pi` the agent spawns via the bash tool, never runs its
handlers. This repo's two extensions are the opposite by design: pi auto-discovers
`.pi/extensions/` from the project (it is one of the cwd trust inputs pi asks about), so any pi
that runs *here* — managed or not — is held to the guards and the step discipline. Normal pi
behavior and the pi SDK are untouched either way.

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
  call and type anything into it. Closed by `features.unified_exec = false` in
  `.mngr/settings.toml`. It is NOT closable via the `tools` table: `ToolsToml` accepts only
  `web_search` / `experimental_request_user_input` / `update_plan`, and any other key there is
  silently ignored.
- **agy `manage_task` `send_input` (open).** Stdin piped into a task that a guarded
  `run_command` launched is not itself a tool call, so nothing sees it. No event exists to hook.
- **agy nested shells (open, deliberate).** Only the outermost `bash -c` is guarded; policing the
  whole tree would block third-party build scripts and re-apply the rewrite per level.
- **pi nested or plain `pi` (open).** mngr's lifecycle extension loads only via `-e` on a managed
  launch, so a nested pi gets no P1/P2/P4. Worse, `.pi/extensions/` is **trust-gated**: pi loads
  project extensions only `if (projectTrusted)`, and trust resolves to false for a
  non-interactive run with no stored decision -- so a nested `pi -p` started from a subdirectory
  gets NO guards at all. mngr seeds trust for the work dir, which covers the normal case.
- **`sh -c` / `eval` (open on every harness, including claude).** The blockers anchor on the
  command text, so re-entering through another interpreter evades them by construction.

## Keeping the harnesses in step

When a rule changes, update every harness that carries it:
- **Safety 1–2** (`agent_block_pipe_tail_head.sh`, `agent_prevent_commit_rewrite.sh`): the
  scripts (shared by claude **and** codex) and `commandBlockReason()` in mngr's
  `mngr_pi_lifecycle.ts` (pi) — these hold for any pi agent, so mngr still carries them.
- **Workflow 5, 7–8** (`agent_require_steps_pretool.sh`, `agent_open_tickets_reminder.sh`,
  `agent_open_tickets_stop_nudge.sh`): the scripts (claude **and** codex) and the matching
  handler in **this repo's** `.pi/extensions/tk_workflow.ts` (pi). The step discipline is
  this repo's, not mngr's — mngr's lifecycle extension no longer carries any of it.
- **Safety 3** (`agent_latchkey_request_check.py`) and **workflow 6**
  (`agent_tk_standalone_check.py`): one checker file each, reached by claude and codex through
  their `.sh` wrappers and called directly by pi — so the tokenizing rule is single-sourced.
- **Safety 4** (`agent_rewrite_bash_command.py`): shared by claude and codex; pi mirrors its
  prefix logic in `rewriteBashCommand()`. Keep it **last** in both hook configs, and keep
  mngr recording `mngrOriginalCommand` for pi — a blocker that inspects the rewritten
  command refuses everything (see "Which command a guard sees" above).
- codex, pi and agy wiring lives in **this repo**: `.codex/hooks.json`,
  `.pi/extensions/policy_guards.ts` and `system/scripts/agy_shim/bash`. A guard added to
  `.claude/settings.json` needs the matching entry in all three, and nothing in mngr. agy is
  the cheapest of the three: adding a guard to the shim's loop is one line, because it runs
  the claude script itself. mngr's only contribution is the PATH entry.
- claude and codex share one runtime (shell + JSON) so they share files; pi is a separate
  runtime (in-process TypeScript), so its copy is unavoidable — but small, and its rules and
  reminder text are verbatim copies.
