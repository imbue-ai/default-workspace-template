# Agent policy hooks — how the guards work across claude / codex / pi / agy

Minds runs a set of **hooks** on its agents: checkpoints fired around a tool call or a
turn where we can block a command, rewrite it, or inject a reminder into the model's
context. claude is the reference harness — it runs every hook here. This file is the single
source of truth for **each hook claude runs and how codex and pi reflect it** (or why one is
deliberately claude-only). The hook *logic* lives in the `agent_*` scripts in this directory;
codex reuses those scripts verbatim, pi runs their checkers or re-expresses them in TypeScript.

## Status legend

- **live** — wired and in effect on that harness today.
- **planned** — the channel is verified (see the contract tables below) but the wiring is not
  in place yet.
- **n/a** — intentionally not ported; the "claude-only" section explains why.

## At a glance

| # | Hook | Event | Kind | claude | codex | pi | agy |
|---|------|-------|------|--------|-------|----|-----|
| 1 | Block a command piping into `tail`/`head` | PreToolUse | safety | live | live | live | live |
| 2 | Block `git rebase` / `commit --amend`/`--fixup` / `pull --rebase` | PreToolUse | safety | live | live | live | live |
| 3 | Block a latchkey permission request that is batched, chained, or redirected | PreToolUse | safety | live | live | live | live |
| 4 | Rewrite every Bash command: OOM self-tag + git identity | PreToolUse | safety | live | live | live | live |
| 5 | Nudge when doing substantive work with no in-progress step | PreToolUse | workflow | live | live | live | n/a |
| 6 | Block a `tk start`/`close` that is chained or redirected | PreToolUse | workflow | live | live | live | live |
| 7 | Carry over still-open steps into the next turn | UserPromptSubmit | workflow | live | live | live | n/a |
| 8 | Surface steps the previous turn left open | Stop (pi: at turn-start, via #7) | workflow | live | live | live | n/a |
| 9 | Session-start setup (`uv sync`, tk-on-path, plugin update, shed notice) | SessionStart | setup | live | n/a | n/a | n/a |
| 10 | Force the agent back to the repo root before it stops | Stop | workflow | live | n/a | n/a | n/a |

Hooks 1–4 (safety) and 5–8 (tk workflow discipline) are the cross-harness set. Hooks 9–10 are
claude-only by construction — see the last section. Note the **Stop** event: claude runs two Stop
hooks (#8 open-items, #10 cwd); on pi neither *reaches the agent* on stop — #10 does not apply, and
#8's agent-visible reminder is delivered at the **start of the next turn** (#7) because pi's stop
event (`agent_settled`) can only write to stderr. pi still registers an `agent_settled` handler
for #8, but it is a stderr-only log (clobbered in the TUI), not a channel to the agent.

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
**#3 is fully live**: agy sets `WaitMsBeforeAsync` on every `run_command` and runs the child
synchronously, so there is no agent-controllable background flag and the `--backgrounded` arm
has nothing to detect.

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

**Which command a guard sees.** On claude and codex, the rewriter (#4) is deliberately the
**last** PreToolUse hook, so every blocker ahead of it inspects the command the agent wrote.
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

## Category A — shell-command safety policies (live in all three)

### 1. Block pipe into `tail`/`head` — `agent_block_pipe_tail_head.sh`
Redirect to a file and read that instead.
- **claude / codex**: the script — matches `\|\s*(tail|head)`, writes the reason to stderr, `exit 2`.
- **pi**: same regex in `commandBlockReason()`; returns `{block, reason}`.

### 2. Block git history rewrites — `agent_prevent_commit_rewrite.sh`
Blocks `git rebase`, `git commit --amend|--fixup`, `git pull --rebase`.
- **claude / codex**: the script (stderr + `exit 2`).
- **pi**: the same set of regexes in `commandBlockReason()`.

### 3. Block a batched/chained/redirected permission request — `agent_latchkey_request_standalone.sh`
A **hard** block when a POST to the reserved `latchkey-self.invalid/permission-requests` host
(the call that FILES a permission request) shares its tool call with a second request, with
another command, has its output redirected, or runs in the background. The chat builds the card the user acts on out of
that one call: only the first echoed request object in the result is read, and one card is
rendered per call — so a second request is never shown, and `> /tmp/req.json` / `| jq
.request_id` takes the echoed object away, leaving the card with no button. Every other latchkey
call, including reading the queue, is untouched. The tokenizing lives in
`agent_latchkey_request_check.py` (`shlex` again, so a rationale that mentions `&&` or `>` stays
inside its quoted token).

The redirect half is blunt on purpose: `CommandSegment.has_redirect` records only *that* a
segment is redirected, so an *input* redirect (`-d @- < body.json`, or a heredoc, whose body
also re-enters the parse as further commands) is blocked alongside the output ones, even though
it leaves the echo intact. The block message names that form, and the fix is the same either
way — pass the body inline with `-d '{...}'`.

One violation is not in the command text at all: a **backgrounded** tool call
(claude's Bash `run_in_background: true`) returns a shell id, so the echo lands in a later
`BashOutput` call rather than in the card's own result — the same failure as a trailing `&`,
which the command-text checks already block. The `.sh` therefore also reads
`.tool_input.run_in_background` out of the payload and passes `--backgrounded` to the `.py`,
which blocks when a request is filed that way. A harness whose payload has no such field never
sets it.
- **claude / codex**: the `.sh`, which execs the `.py`; stderr + `exit 2` blocks.
- **pi**: `on("tool_call")` runs the **same** `agent_latchkey_request_check.py` synchronously
  (only when the command mentions the host) and maps its exit-2/stderr to `{block, reason}` —
  the same bridge shape as the tk-standalone checker below. It passes the command alone: the
  `--backgrounded` flag has no counterpart in pi's `tool_call` input.

### 4. Rewrite every Bash command — `agent_rewrite_bash_command.py`
Prepends an OOM self-tag (so the agent's subprocesses are shed first under memory pressure)
and the agent's git identity (`GIT_AUTHOR_*`/`GIT_COMMITTER_*`), then runs the original
command verbatim.
- **claude**: the script prints `hookSpecificOutput.updatedInput.command`.
- **codex**: the **same** script, invoked with the `--codex` flag. Codex rejects an
  `updatedInput` that has no `permissionDecision`, so the flag makes the script also emit
  `permissionDecision: "allow"`. claude runs it without the flag, because in claude a
  PreToolUse `allow` would auto-approve the tool and skip the permission prompt. The `allow`
  does **not** weaken hooks 1–3: they are earlier PreToolUse hooks and codex honors an earlier
  block over a later allow (verified live — a blocked `git commit --amend` / `| head` never
  runs even with the rewriter allowing).
- **pi**: `rewriteBashCommand()` mutates `event.input.command` with the same OOM + identity
  prefix.

## Category B — tk workflow-discipline policies

These enforce the `tk` step discipline that the chat progress view is built from, and are now
**live on all three** harnesses (codex and pi read the same shared `AGENTS.md` that mandates
`tk`). codex reuses the exact claude scripts; pi re-expresses them against its SDK. Two
codex-specific output-channel quirks were found and handled while wiring these (noted inline
below): the carryover reminder needs a `--codex` flag, and the stop nudge must never exit
non-zero.

### 5. Require a step before substantive work — `agent_require_steps_pretool.sh`
A **soft** reminder (never blocks) when a substantive tool call happens with no in-progress
step. Skipped for read-only tools (`Read`/`Glob`/`Grep`/…) and for Bash commands that invoke
`tk` itself.
- **claude**: PreToolUse, prints `hookSpecificOutput.additionalContext` (exit 0).
- **codex**: the **same** script — codex honors PreToolUse `additionalContext` identically, so
  it reuses it with no change.
- **pi**: `on("tool_result")` — skip `read`/`grep`/`find`/`ls` and `tk …` bash; if no
  in-progress step, append the same reminder text to the result `content` (pi's `tool_call`
  result cannot inject non-blocking context, so the reminder rides the tool result instead —
  same visible effect, one tool-round later).

### 6. Block a non-standalone `tk start`/`close` — `agent_tk_standalone.sh`
A **hard** block when a `tk start`/`close` is chained (`cd …;`, `&&`, `|`, …) or redirected,
which would drop the step's transition out of the progress view. `create` is exempt. The
tokenizing lives in `agent_tk_standalone_check.py` (uses `shlex`, which a bash regex can't do
reliably).
- **claude / codex**: the `.sh`, which execs the `.py`; stderr + `exit 2` blocks. Codex honors
  the exit-2 block identically.
- **pi**: `.pi/extensions/policy_guards.ts` runs the **same** `agent_tk_standalone_check.py`
  synchronously from `on("tool_call")` — through `bash --noprofile --norc -c`, with the command
  passed as `$1`, and only when the command mentions `tk`/`ticket` — and maps its exit-2/stderr
  to `{block, reason}`; reusing the checker keeps the shlex tokenizer single-sourced. Step state
  for the other guards comes from `spawnSync("bash", [ticket, ...])` in
  `.pi/extensions/tk_workflow.ts` (via `bash` so it runs regardless of the mount's exec bit).

### 7. Carry over open steps into the next turn — `agent_open_tickets_reminder.sh`
When a new user message arrives and this agent has still-open step records, inject a reminder
listing them so the agent reconciles before acting.
- **claude**: UserPromptSubmit, prints the reminder to stdout (added to context).
- **codex**: the **same** script, invoked with `--codex`. codex adds a UserPromptSubmit hook's
  plain stdout to context — *except* it first tries to JSON-parse stdout that begins with `[`
  or `{`, and this reminder starts with `[Open task reminder...]`, so plain text is rejected as
  a hook failure (verified live). `--codex` makes the script emit the reminder as
  `hookSpecificOutput.additionalContext` JSON instead, which codex adds to context cleanly.
- **pi**: `on("before_agent_start")` — if `tk steps` reports steps the previous turn left open,
  return `{systemPrompt: base + reminder}` to **append the reminder to this turn's system prompt**,
  the guaranteed model-visible channel (`BeforeAgentStartEventResult` accepts either `message` or
  `systemPrompt`; we use `systemPrompt`, and pi resets the override each turn). This is also where pi does the
  "steps left open" surfacing that claude runs as a Stop hook (#8): pi has no usable stop-time
  channel to the agent, so both the carryover and the leftover-open reminder are delivered here,
  at the start of the next turn.

### 8. Nudge on stop with open steps — `agent_open_tickets_stop_nudge.sh`
A non-blocking, log-only note (exit 0 always) when the agent stops with steps still open.
Real follow-up is handled by hook 7 on the next turn.
- **claude**: Stop, writes to stderr, `exit 0`.
- **codex**: the **same** script — stderr only, `exit 0`, no stdout. Codex accepts an
  empty-stdout, exit-0 Stop hook as a clean no-op (confirmed live on 0.146.0 and against the
  generated `stop.command.output` schema, which has no `additionalContext` and defaults to a
  normal stop). This exposed a latent bug: `tk steps` exits non-zero when there are no steps,
  which under `set -euo pipefail` made the script exit non-zero — and on codex an exit 2 on
  Stop is a *continuation* request that re-engages the agent. The script now guards that count
  with `|| true`, honoring its "exits 0 always" contract on every harness.
- **pi**: **not done as a stop hook.** pi's stop-time event (`agent_settled`) is a pure
  notification whose return value is ignored (registered as `ExtensionHandler<AgentSettledEvent>`
  with no result type), so a handler can only write to stderr — and in pi's full-screen TUI,
  running in a tmux pane, that stderr is clobbered and never reaches the agent or the chat. So on
  pi the "steps left open" reminder is surfaced at the **start of the next turn** via
  `before_agent_start` (see #7), the same channel the carryover uses — not on stop. pi does
  register an `agent_settled` handler here, but only as a stderr log; it is not the agent-visible
  reminder (that rides #7).

## Category C — the Stop event, and claude-only hooks (by construction)

Claude fires **two** hooks on the **Stop** event: the open-items nudge (#8, above) and the
return-to-repo-root cwd check (#10, below). On **pi**, neither runs as a Stop hook — #10 does not
apply (pi has no persistent cwd), and #8 is folded into the **turn-start** check (#7), because
pi's stop event cannot reach the agent. **codex** runs #8 as a real Stop hook, but not #10 (same
cwd reasoning). So of claude's two Stop hooks, only #8's *purpose* is cross-harness, and pi
delivers it at turn start rather than on stop.

### 9. Session-start setup — SessionStart
`uv sync --all-packages`, `ensure_tk_on_path.sh`, `claude_update_plugin.sh` (a Claude-Code
*plugin* updater with no codex/pi analogue), and the OOM shed-notice hook. Provisioning is each
harness's own concern: codex/pi get their environment from their plugins, and `tk` is already
baked onto `PATH` in the image (and the hooks call the vendored `ticket` script by absolute
path anyway), so there is nothing to port here.

### 10. Return-to-repo-root before stop — Stop
claude blocks the stop (`exit 2`) until the agent `cd`s back to the repo root, so the *other*
Stop hooks resolve paths correctly. This does not port:
- **codex** resets the shell cwd per command (each Bash call starts at the work dir), so there
  is no persistent cwd to police — and on codex `exit 2` on Stop *continues* the agent rather
  than holding it, which is the wrong action entirely.
- **pi** runs the bash tool against `ctx.cwd`, not a persistent shell, so the same reasoning
  applies.

## Keeping the three in step

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
