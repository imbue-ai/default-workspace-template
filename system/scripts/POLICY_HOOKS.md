# Agent policy hooks — how the guards work across claude / codex / pi

Minds runs a set of **hooks** on its agents: checkpoints fired around a tool call or a
turn where we can block a command, rewrite it, or inject a reminder into the model's
context. claude is the reference harness — it runs every hook here. This file is the single
source of truth for **each hook claude runs and how codex and pi reflect it** (or why one is
deliberately claude-only). The hook *logic* lives in the `claude_*` scripts in this directory;
codex reuses those scripts verbatim, pi re-expresses them in TypeScript.

## Status legend

- **live** — wired and in effect on that harness today.
- **planned** — the channel is verified (see the contract tables below) but the wiring is not
  in place yet.
- **n/a** — intentionally not ported; the "claude-only" section explains why.

## At a glance

| # | Hook | Event | Kind | claude | codex | pi |
|---|------|-------|------|--------|-------|----|
| 1 | Block a command piping into `tail`/`head` | PreToolUse | safety | live | live | live |
| 2 | Block `git rebase` / `commit --amend`/`--fixup` / `pull --rebase` | PreToolUse | safety | live | live | live |
| 3 | Rewrite every Bash command: OOM self-tag + git identity | PreToolUse | safety | live | live | live |
| 4 | Nudge when doing substantive work with no in-progress step | PreToolUse | workflow | live | live | live |
| 5 | Block a `tk start`/`close` that is chained or redirected | PreToolUse | workflow | live | live | live |
| 6 | Carry over still-open steps into the next turn | UserPromptSubmit | workflow | live | live | live |
| 7 | Log a stop that leaves steps open | Stop | workflow | live | live | live |
| 8 | Session-start setup (`uv sync`, tk-on-path, plugin update, shed notice) | SessionStart | setup | live | n/a | n/a |
| 9 | Force the agent back to the repo root before it stops | Stop | workflow | live | n/a | n/a |

Hooks 1–3 (safety) and 4–7 (tk workflow discipline) are the cross-harness set. Hooks 8–9 are
claude-only by construction — see the last section.

## How each harness attaches

### claude  (wiring: `.claude/settings.json`)
Runs the `.sh`/`.py` scripts in this directory, one entry per hook under the matching event
(`PreToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`). A script reads the event JSON on
stdin. This is the reference implementation and needs no changes.

### codex  (wiring: `mngr_codex/.../codex_config.py` → `build_codex_hooks_config()`)
Codex speaks the **same hook protocol** as claude — same event names, same stdin payload
(`tool_name`, `.tool_input.command`, claude-shaped even under code mode), same output
channels — so codex **reuses the exact same scripts**, referenced from the work dir
(`$MNGR_AGENT_WORK_DIR/system/scripts/…`). No copy, no new logic: editing a script updates
claude and codex at once. `build_codex_hooks_config` just adds the entries. Codex requires its
command hooks be trusted; the plugin passes `--dangerously-bypass-hook-trust` (consent-gated)
so mngr's own hooks run.

### pi  (wiring: `mngr_pi_coding/.../resources/mngr_pi_lifecycle.ts`)
Pi has **no shell-hook surface** — its only extension point is a TypeScript module loaded with
`pi -e <per-agent-path>`. So pi **cannot run the scripts**; it re-expresses each rule against
the pi SDK event that matches. Reminder *text* and regexes are copied verbatim from the scripts
so all three harnesses read identically; where a rule needs tk state, pi shells out to the same
vendored `ticket` binary the scripts use. The SDK is documented in the package's
`dist/core/extensions/types.d.ts`; we do NOT modify it.

**Minds-only, by construction.** The pi extension is loaded via the `-e` flag mngr adds *only*
to a managed agent's launch command (`plugin.py::assemble_command`). A user running plain `pi`
never loads it, and a nested `pi` the agent spawns via the bash tool (no `-e`) never runs these
handlers. Normal pi behavior and the pi SDK are untouched.

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

### 1. Block pipe into `tail`/`head` — `claude_block_pipe_tail_head.sh`
Redirect to a file and read that instead.
- **claude / codex**: the script — matches `\|\s*(tail|head)`, writes the reason to stderr, `exit 2`.
- **pi**: same regex in `commandBlockReason()`; returns `{block, reason}`.

### 2. Block git history rewrites — `claude_prevent_commit_rewrite.sh`
Blocks `git rebase`, `git commit --amend|--fixup`, `git pull --rebase`.
- **claude / codex**: the script (stderr + `exit 2`).
- **pi**: the same set of regexes in `commandBlockReason()`.

### 3. Rewrite every Bash command — `claude_rewrite_bash_command.py`
Prepends an OOM self-tag (so the agent's subprocesses are shed first under memory pressure)
and the agent's git identity (`GIT_AUTHOR_*`/`GIT_COMMITTER_*`), then runs the original
command verbatim.
- **claude**: the script prints `hookSpecificOutput.updatedInput.command`.
- **codex**: the **same** script, invoked with the `--codex` flag. Codex rejects an
  `updatedInput` that has no `permissionDecision`, so the flag makes the script also emit
  `permissionDecision: "allow"`. claude runs it without the flag, because in claude a
  PreToolUse `allow` would auto-approve the tool and skip the permission prompt. The `allow`
  does **not** weaken hooks 1–2: they are earlier PreToolUse hooks and codex honors an earlier
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

### 4. Require a step before substantive work — `claude_require_steps_pretool.sh`
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

### 5. Block a non-standalone `tk start`/`close` — `claude_tk_standalone.sh`
A **hard** block when a `tk start`/`close` is chained (`cd …;`, `&&`, `|`, …) or redirected,
which would drop the step's transition out of the progress view. `create` is exempt. The
tokenizing lives in `claude_tk_standalone_check.py` (uses `shlex`, which a bash regex can't do
reliably).
- **claude / codex**: the `.sh`, which execs the `.py`; stderr + `exit 2` blocks. Codex honors
  the exit-2 block identically.
- **pi**: `on("tool_call")` runs the **same** `claude_tk_standalone_check.py` synchronously
  (`spawnSync("python3", [checker, command])`, only when the command mentions `tk`/`ticket`) and
  maps its exit-2/stderr to `{block, reason}` — reusing the checker keeps the shlex tokenizer
  single-sourced. Step state for the other guards comes from `spawnSync("bash", [ticket, ...])`
  (via `bash` so it runs regardless of the mount's exec bit).

### 6. Carry over open steps into the next turn — `claude_open_tickets_reminder.sh`
When a new user message arrives and this agent has still-open step records, inject a reminder
listing them so the agent reconciles before acting.
- **claude**: UserPromptSubmit, prints the reminder to stdout (added to context).
- **codex**: the **same** script, invoked with `--codex`. codex adds a UserPromptSubmit hook's
  plain stdout to context — *except* it first tries to JSON-parse stdout that begins with `[`
  or `{`, and this reminder starts with `[Open task reminder...]`, so plain text is rejected as
  a hook failure (verified live). `--codex` makes the script emit the reminder as
  `hookSpecificOutput.additionalContext` JSON instead, which codex adds to context cleanly.
- **pi**: `on("before_agent_start")` — if `tk steps` reports open steps, return
  `{systemPrompt: event.systemPrompt + "\n\n" + reminder}` (the guaranteed-visible channel).

### 7. Nudge on stop with open steps — `claude_open_tickets_stop_nudge.sh`
A non-blocking, log-only note (exit 0 always) when the agent stops with steps still open.
Real follow-up is handled by hook 6 on the next turn.
- **claude**: Stop, writes to stderr, `exit 0`.
- **codex**: the **same** script — stderr only, `exit 0`, no stdout. Codex accepts an
  empty-stdout, exit-0 Stop hook as a clean no-op (confirmed live on 0.146.0 and against the
  generated `stop.command.output` schema, which has no `additionalContext` and defaults to a
  normal stop). This exposed a latent bug: `tk steps` exits non-zero when there are no steps,
  which under `set -euo pipefail` made the script exit non-zero — and on codex an exit 2 on
  Stop is a *continuation* request that re-engages the agent. The script now guards that count
  with `|| true`, honoring its "exits 0 always" contract on every harness.
- **pi**: `on("agent_settled")` — the true "run fully settled" signal; stderr only.

## Category C — claude-only, not ported (by construction)

### 8. Session-start setup — SessionStart
`uv sync --all-packages`, `ensure_tk_on_path.sh`, `claude_update_plugin.sh` (a Claude-Code
*plugin* updater with no codex/pi analogue), and the OOM shed-notice hook. Provisioning is each
harness's own concern: codex/pi get their environment from their plugins, and `tk` is already
baked onto `PATH` in the image (and the hooks call the vendored `ticket` script by absolute
path anyway), so there is nothing to port here.

### 9. Return-to-repo-root before stop — Stop
claude blocks the stop (`exit 2`) until the agent `cd`s back to the repo root, so the *other*
Stop hooks resolve paths correctly. This does not port:
- **codex** resets the shell cwd per command (each Bash call starts at the work dir), so there
  is no persistent cwd to police — and on codex `exit 2` on Stop *continues* the agent rather
  than holding it, which is the wrong action entirely.
- **pi** runs the bash tool against `ctx.cwd`, not a persistent shell, so the same reasoning
  applies.

## Keeping the three in step

When a rule changes, update every harness that carries it:
- **Safety 1–2** and **workflow 4–6**: the `claude_*` scripts (shared by claude **and** codex)
  and the matching handler in `mngr_pi_lifecycle.ts` (pi).
- **Safety 3** (`claude_rewrite_bash_command.py`) and **workflow 5** checker
  (`claude_tk_standalone_check.py`): shared by claude and codex; pi calls #5's checker directly
  and mirrors #3's prefix logic in `rewriteBashCommand()`.
- codex wiring lives in `build_codex_hooks_config()`; pi wiring in the `pi.on(...)` handlers.
- claude and codex share one runtime (shell + JSON) so they share files; pi is a separate
  runtime (in-process TypeScript), so its copy is unavoidable — but small, and its rules and
  reminder text are verbatim copies.
