# System-prompt stopgaps for tool restrictions we can't enforce technically

Items where no config/CLI-level block exists (or isn't confirmed), so the
only current mitigation is telling the agent not to do it — advisory, not
enforced. Each note also says whether a real enforced fix might exist
instead, and where it'd belong if so.

## codex

- **Don't use `update_plan` — use `tk` for step tracking instead.** No
  feature flag exists to disable `update_plan` (unlike `multi_agent`/`apps`,
  which are real toggles). Stopgap: system-prompt instruction.
  **Possible real fix (unconfirmed):** codex supports `PreToolUse` hooks
  with a documented deny schema (`hookSpecificOutput.permissionDecision:
  "deny"`), but the docs only explicitly listed Bash/apply_patch/MCP tool
  coverage — whether `update_plan` calls even reach a `PreToolUse` hook was
  never verified. If confirmed, belongs in fct's own project-level
  `~/.codex/hooks.json` (same layer as `.claude/settings.json`'s hooks for
  claude), not in the `mngr_codex` plugin.

## antigravity

- **Don't use any built-in interactive-question or task-tracking tool if
  one exists — use `tk` for step tracking, never prompt the user.** Vaguer
  than the codex note because antigravity's real built-in tool names could
  not be enumerated (checked `agy --help`, all subcommands, settings.json
  schema, binary strings — genuinely unknown, not just undocumented).
  Stopgap: system-prompt instruction, necessarily imprecise since we can't
  name the specific tool(s) to avoid.
  **Possible real fix:** same `PreToolUse` hook mechanism agy supports
  (confirmed to exist), blocked purely on not knowing tool names to match
  against. Not a "which layer" question yet — no layer can implement this
  without that information first.

## opencode

- **No stopgap needed.** Real, working, config-level enforcement already
  exists (`tools = {"todowrite": false, "question": false, "task": false}`
  in `[agent_types.opencode]`) — this is an actual block, not advisory.
