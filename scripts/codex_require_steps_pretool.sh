#!/usr/bin/env bash
# PreToolUse hook (codex): soft-block substantive tool calls when the agent
# has no in_progress step record. Direct port of
# scripts/claude_require_steps_pretool.sh -- codex's PreToolUse hook is
# confirmed to use the same stdin shape (tool_name, tool_input.command) and
# the same non-blocking hookSpecificOutput.additionalContext injection
# mechanism as Claude Code (per developers.openai.com/codex/hooks), so the
# logic ports almost verbatim; only the tool-name skip-list changed to
# codex's own built-ins.
#
# Skipped for non-substantive tools (update_plan -- codex's own plan/todo
# tool, and view_image, a read-only op) and for shell/Bash commands that
# invoke tk itself. codex's confirmed built-ins are shell, exec_command,
# write_stdin, apply_patch, update_plan, view_image -- but the official
# hooks doc's own PreToolUse example matches "^Bash$", suggesting the
# hook-facing tool_name may be aliased to "Bash" for Claude-Code
# compatibility rather than the internal "shell" name. This script treats
# both names as the same tool defensively, since which one actually shows
# up was not independently re-confirmed live.
set -euo pipefail

emit_reminder() {
    jq -n --arg ctx "$1" \
        '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $ctx}}'
    exit 0
}

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

input=$(cat)

tool_name=$(echo "$input" | jq -r '.tool_name // empty')

# Tools that don't count as "substantive work."
case "$tool_name" in
    update_plan|view_image)
        exit 0
        ;;
esac

# For shell/Bash calls, skip if the command is invoking tk (creating/managing
# steps). Regex match (not a case/glob), matching .opencode/plugin/require-steps.ts's
# /(^|\/|\s)(tk|ticket)\s/ exactly: "tk"/"ticket" at the very start, after a
# slash, or after whitespace, followed by whitespace. Fixes two real bugs the
# old glob (tk\ *|*/tk\ *|*/ticket\ *|*tk\ *) had (found by code review):
# (1) the catch-all `*tk\ *` alternative matched any command merely
# CONTAINING "tk " anywhere -- e.g. `apt-get install -y python3-tk` -- a
# false-negative that silently skipped the check; (2) no bare `ticket ...`
# alternative existed (only `/ticket ...`), so `ticket create --step "..."`
# with no leading slash was never recognized, causing a false-positive
# reminder on the very call that was managing steps.
if [[ "$tool_name" == "Bash" || "$tool_name" == "shell" ]]; then
    command=$(echo "$input" | jq -r '.tool_input.command // empty')
    if [[ "$command" =~ (^|/|[[:space:]])(tk|ticket)[[:space:]] ]]; then
        exit 0
    fi
fi

[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

export TICKETS_DIR="$tickets_dir"

in_progress=$("$tk_script" steps --status=in_progress 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

if [[ -n "$in_progress" ]]; then
    exit 0
fi

open_steps=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

if [[ -n "$open_steps" ]]; then
    emit_reminder "
[Step tracking reminder]

You have declared step records but none is currently in_progress. Call \`tk start <id>\` on your next step before doing more work. Steps must be serial -- only one in_progress at a time.
"
fi

emit_reminder "
[Step tracking reminder]

You are about to do work without declaring any step records. The chat progress view requires steps to render your work as a structured timeline.

Before continuing, declare your plan as step records (each prints \`Created <id>: <title>\`):
  tk create --step \"Description of first step\"
  tk create --step \"Description of second step\"
  ...
Then start the first step with its literal id: tk start <id>

See AGENTS.md > Task management for the full protocol.
"
