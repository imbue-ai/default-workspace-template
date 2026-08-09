# Agent policy hooks — how the guards work across claude / codex / pi

Minds enforces a few shell-command **policies** on its agents (e.g. "don't pipe through
`tail`/`head`", "don't `git commit --amend`", "tag every command for OOM + git identity").
This file is the single source of truth for how those policies are implemented in each of
the three harnesses. The policy *logic* lives in the scripts in this directory.

## The policies (v1)

| # | Policy | Action | Source |
|---|---|---|---|
| 1 | Block a command that pipes into `tail`/`head` | block | `claude_block_pipe_tail_head.sh` |
| 2 | Block `git rebase`, `git commit --amend/--fixup`, `git pull --rebase` | block | `claude_prevent_commit_rewrite.sh` |
| 3 | Prefix every Bash command with an OOM self-tag + the agent's git identity | rewrite | `claude_rewrite_bash_command.py` |

(Other claude hooks — the `tk` step reminders, session-start setup — are claude-workflow
discipline, not cross-harness safety, and are deliberately NOT ported. See the bottom.)

## How each harness runs them

A "hook" is a checkpoint fired **before a tool runs** where we can **block** the command or
**rewrite** it. All three harnesses have this checkpoint; they differ only in how you attach
to it.

### claude  (wiring: `.claude/settings.json` → `PreToolUse`)
Runs the `.sh`/`.py` scripts in this directory. A script reads the tool call as JSON on
stdin (`.tool_input.command`), and to **block** writes a reason to stderr and `exit 2`; to
**rewrite** it prints `hookSpecificOutput.updatedInput.command`. This is the reference
implementation and needs no changes.

### codex  (wiring: `mngr_codex/.../codex_config.py` → `build_codex_hooks_config()`)
Codex speaks the **same hook protocol** as claude: same `PreToolUse` event, same payload
(`tool_name:"Bash"`, `.tool_input.command`, verified live under code-mode), same block
convention (stderr + `exit 2`) and rewrite channel (`updatedInput`). So codex **reuses the
exact same scripts** — the plugin just adds a `PreToolUse` entry pointing at them via
`$MNGR_AGENT_WORK_DIR/system/scripts/…`. No new logic. Editing a script updates claude AND
codex at once.

**One protocol divergence (codex ≥ ~0.146):** a hook that returns `updatedInput` must also
carry an explicit `permissionDecision: "allow"` in the same `hookSpecificOutput`, or codex
rejects it — `PreToolUse hook returned updatedInput without permissionDecision:allow` — and
runs nothing. Only the rewriter (`claude_rewrite_bash_command.py`) returns `updatedInput`, so
only it is affected. It takes a `--codex` flag that adds the `allow` decision; codex wires it
with the flag, claude without (in claude a PreToolUse `allow` would auto-approve the tool and
skip the permission prompt). The `allow` does **not** weaken the block guards: they are
separate earlier `PreToolUse` hooks and codex honors an earlier block over a later allow
(verified live — a blocked `git commit --amend` / `| head` does not run even with the
rewriter allowing).

### pi  (wiring: `mngr_pi_coding/.../resources/mngr_pi_lifecycle.ts` → `pi.on("tool_call")`)
Pi has **no shell-hook surface** — its only extension point is a TypeScript handler loaded
into the pi process. So pi **cannot run the `.sh` scripts**; it re-expresses the same rules
in ~20 lines of TS in the lifecycle extension: return `{block:true, reason}` to block, or
mutate `event.input.command` to rewrite. The rules are a copy of the scripts' regexes (they
change ~never); if that ever becomes a maintenance issue, extract the patterns to a shared
JSON both sides read. The pi SDK API used (`tool_call` event, mutable `event.input`,
`{block,reason}` result) is documented in the package's
`dist/core/extensions/types.d.ts`; we do NOT modify the SDK.

**Minds-only, by construction.** The pi extension is loaded via `pi -e <per-agent-path>`
that mngr adds *only* to the launch command for a managed agent (`plugin.py::assemble_command`).
A user running plain `pi` never loads it. Normal pi behavior and the pi SDK are untouched.

## Why the logic isn't identical in all three files

claude and codex share one runtime (shell + JSON), so they share the scripts. pi is a
different runtime (in-process TypeScript), so its copy is unavoidable — but small. The three
files enforce the same rules; keep them in step when a rule changes:
- `system/scripts/claude_block_pipe_tail_head.sh` / `claude_prevent_commit_rewrite.sh` (claude + codex)
- the `pi.on("tool_call")` handler in `mngr_pi_lifecycle.ts` (pi)

## Deliberately NOT ported to codex/pi (v1)
`claude_require_steps_pretool.sh`, `claude_tk_standalone.sh`, `claude_open_tickets_*` — these
enforce the `tk` chat-progress-view discipline and are gated on claude-only env; only port
them once we confirm codex/pi should drive that same timeline. `claude_update_plugin.sh` is a
Claude-Code-plugin updater with no codex/pi equivalent. Session-start env setup is handled by
each harness's own provisioning.
