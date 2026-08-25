# agy policy parity: the bash shim (BUILT -- see POLICY_HOOKS.md for the live reference)

How antigravity gets the guards in `system/scripts/POLICY_HOOKS.md`, given that its hook
surface cannot carry them.

## 0. Why not hooks

Measured against agy 1.1.20 by wiring a probe on every event name in the binary and running a
real tool call:

| what | result |
|---|---|
| events that fire | `SessionStart`, `PreInvocation`, `PostInvocation`, `Stop` |
| events declared but NEVER delivered | **`PreToolUse`, `PostToolUse`** |
| payload | conversation-scoped only: `conversationId`, `invocationNum`, `modelName`, `transcriptPath`, `workspacePaths`, `artifactDirectoryPath` (+ `error`, `executionNum`, `fullyIdle`, `terminationReason` on `Stop`) |
| tool identity in the payload | **none** -- no `tool_name`, no `tool_input`, no command |
| output that reaches the model | **none** -- plain stdout AND the binary's own `systemMessage` key both tested on `PreInvocation`; the model answered `NO` when asked if it could see the marker |

Six of the eight cross-harness hooks need `PreToolUse`; #7 needs `UserPromptSubmit`, which agy
does not have at all. So the hook route carries nothing, and this is Google's CLI -- not ours
to fix.

## 1. The lever

agy executes `run_command` as **`bash -c "<CommandLine>"`, resolving `bash` from `PATH`**.
Verified with a PATH shim that logged `SHIM-INTERCEPTED: -c echo shim-test-marker`.

A shim there provides all three capabilities the hooks were meant to give us:

- **block** -- exit non-zero; stderr becomes the tool result. Verified end to end: a shim
  refusing `git rebase -i HEAD~2` produced, in agy's own transcript,
  `BLOCKED-BY-POLICY: git rebase rewrites history. Make a new commit instead.`
- **rewrite** -- prepend, then `exec` the real bash.
- **inject** -- write to stderr; the model reads it as part of the tool result.

One interposition point where claude needs six hooks.

## 2. What converts

| # | Hook | Mechanism |
|---|---|---|
| 1 | pipe into `tail`/`head` | shim feeds `agent_block_pipe_tail_head.sh` |
| 2 | git history rewrites | shim feeds `agent_prevent_commit_rewrite.sh` |
| 3 | batched latchkey request | shim feeds `agent_latchkey_request_standalone.sh` |
| 6 | chained `tk start`/`close` | shim feeds `agent_tk_standalone.sh` |
| 4 | OOM tag + git identity | shim takes the prefix from `agent_rewrite_bash_command.py --prefix-only` |
| 5 | require-step nudge | **NOT converted** -- see below |

**Reuse is total for the five that convert.** Every one is the file claude already runs; only
the rewriter gained a flag (`--prefix-only`), which shares its one definition of the prefix. The shim's
only new logic is an adapter: synthesise the claude-shaped hook payload those scripts read on
stdin,

```json
{"tool_name": "Bash", "tool_input": {"command": "<CommandLine>"}}
```

and translate their claude-shaped replies back. Editing a guard keeps updating claude, codex
and now agy at once -- the property POLICY_HOOKS.md's "keeping the three in step" section is
built around. pi remains the only harness needing its own copy.

**Not converted, deliberately:**
- **#5 require-step nudge** -- its skip list is keyed on claude TOOL NAMES, and under the shim
  every call is `Bash`. It would nudge agy's read-only shell work (`cat`, `ls`, `grep`) while
  never seeing agy's own edit tool -- wrong in both directions. Its only channel here also
  writes ~600 chars of policy text into the command's own output stream. That discipline lives
  in `AGENTS.md`, which agy demonstrably reads.
- **#7 carryover** -- no `UserPromptSubmit`. pi already treats this as a turn-start nicety
  rather than a guard, and agy has no turn-start channel to the model either.
- **#8 stop nudge** -- `Stop` fires and could run it, but it is log-only on every harness and
  agy's stderr goes to a tmux pane nobody reads. Same conclusion pi reached.
- **#9 / #10** -- claude-only by construction; unchanged.

## 3. Order

Claude's order is load-bearing: the rewriter is deliberately the **last** PreToolUse hook so
every blocker inspects the command the agent actually wrote. POLICY_HOOKS.md spends a section
on pi's inability to guarantee that, and the `mngrOriginalCommand` workaround it needed.

The shim gets it for free, in one function:

```
1. blockers, in claude's order, on the ORIGINAL command
     prevent_commit_rewrite -> block_pipe_tail_head
     -> latchkey_request_standalone -> tk_standalone
   exit 2      ->  print that guard's stderr, exit 2.   (nothing runs)
   other != 0  ->  log it; the guard is broken, not defeated. (fail open)
2. prefix (#4) <- agent_rewrite_bash_command.py --prefix-only
3. exec /bin/bash -c "<prefix><ORIGINAL>" "$@"
```

The command that executes never passes through JSON, so a non-UTF-8 byte cannot become U+FFFD
and a trailing newline cannot be stripped. `"$@"` carries `bash -c CMD name arg...`.

## 4. Failure posture: fail OPEN

This sits on the path of **every command the agent runs**. A shim that dies takes the agent
with it, which is a worse outcome than any single guard being skipped.

So: any internal error -- a missing script, a `jq` that is not installed, a checker that
crashes, a malformed payload -- is caught, logged to a shim log, and falls through to
`exec /bin/bash "$@"`. Only an explicit exit 2 from a checker blocks.

This inverts the usual security default on purpose, and it is the right call: the guards are a
seatbelt on an agent we already trust to run arbitrary commands, not a sandbox boundary.

**Transparency requirements**, since it impersonates `bash`:
- exact argv passthrough (`exec /bin/bash "$@"`), never a re-quoted reconstruction
- preserve the child's exit code
- stdin/stdout/stderr untouched apart from the deliberate stderr writes
- **no interposition unless `$1` is `-c`** -- an interactive `bash`, a `bash script.sh`, or a
  login shell passes straight through

## 5. Scoping

The shim is a file named `bash` early on `PATH`, so it catches everything resolving `bash`
by name in that environment -- including shells the agent spawns.

- The shim is a **repo file** (`system/scripts/agy_shim/bash`). It enforces workspace policy,
  so it must be editable without a mngr release -- POLICY_HOOKS.md's "nothing in mngr" rule.
  The plugin contributes one `PATH` token on the existing `env` prefix in `assemble_command`.
- Set on the launch command, NOT via `modify_env_vars`: that writes the agent env file, which
  the tmux session sources as its default-command, so the shim would follow the USER into
  every terminal they open in the agent's session.
- Only the **outermost** `bash -c` is guarded (`MNGR_AGY_BASH_SHIM` marks the environment).
  Policing the whole process tree would block third-party code the agent never wrote, and pay
  the full checker cost per nesting level. Nested shells being unguarded is already the norm
  pi sets for nested harnesses.
- `MNGR_AGY_SHIM_OFF=1` disables it without a redeploy.
- `/bin/bash` is called by absolute path, so it cannot recurse into itself; the guards are
  invoked through an explicit `/bin/bash` so their own `#!/usr/bin/env bash` cannot either.

## 6. Tests

Unit (shim-level, no agy needed): each guard blocks its own case and passes everything else;
argv/exit-code/stdin transparency; `$1 != -c` passes through untouched; every failure mode
(missing script, missing `jq`, crashing checker, non-JSON reply) falls through to real bash.

Integration: drive the real scripts through the adapter and assert the block *reasons* are
byte-identical to claude's, so the harnesses cannot drift in what they tell the model.

Live (agy, quota permitting): the four blocks refuse and their reason appears in agy's
transcript; a substantive command with no open step carries the nudge; a `git commit` picks up
the agent's identity; `tk create --step` then survives to render a progress timeline.

## 7. Corrections made during the build

Review caught these before they shipped; each is now covered by a test:

- **The shebang.** `#!/usr/bin/env bash`, copied from every other script here, is a fork bomb
  in a file named `bash` on PATH. Now `#!/bin/bash`, asserted by a test.
- **The payload was an agent-triggerable bypass.** Interpolating the command into JSON dies on
  a quote; under the guards' `set -e` that is a non-2 exit, which fail-open passes through --
  so `# "` appended to any command defeated all four guards. Now `jq -Rs`, with a test.
- **Non-UTF-8 corruption.** Taking the executed command back out of the rewriter's JSON
  replaces raw bytes with U+FFFD and strips trailing newlines. The rewriter gained
  `--prefix-only` so the exec path never round-trips.
- **argv past `-c`** was dropped, contradicting the plan's own transparency requirement.
- **No outermost-only marker**: the shim would have policed the agent's whole process tree,
  blocking third-party code and re-applying the prefix per nesting level.
- **Ownership**: the shim is a repo file, not a plugin file, or a guard edit would need a mngr
  release -- which breaks POLICY_HOOKS.md's "nothing in mngr" rule.
- **#5 dropped.** Its skip list is keyed on claude tool names; under the shim it inverts.

Also settled by measurement rather than assumption: agy sets `WaitMsBeforeAsync` on every
`run_command` and runs the child synchronously, so #3's `--backgrounded` arm has nothing to
detect and the guard is fully live rather than partial.

## 8. What this does NOT fix

The original complaint -- **no progress timeline for agy** -- is only *partly* addressed. #5
is a nudge, exactly as on the other harnesses; it does not force a step. If gemini keeps
creating tasks instead of steps, the remaining lever is instruction wording in `AGENTS.md`,
which it already reads.

And the nudge is the weakest of the six here: the blockers are mechanical, but #5 depends on
the model choosing to comply.
