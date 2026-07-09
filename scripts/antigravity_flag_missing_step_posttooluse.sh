#!/usr/bin/env bash
# PostToolUse hook (antigravity, half 1 of 2): after a tool call finishes,
# check tk step state and write a flag file for the PostInvocation half
# (scripts/antigravity_require_steps_postinvocation.sh) to read and act on.
#
# WHY TWO HOOKS: agy's PostToolUse gives the tool name but its output must be
# `{}` (no context-injection field) -- it can only run side effects, not tell
# the model anything. agy's PostInvocation CAN inject text (injectSteps /
# ephemeralMessage) but its input doesn't include which tool just ran. Neither
# hook alone can both see the tool name AND inject a reminder, so this splits
# the job: this hook decides IF a reminder is needed and records why; the
# PostInvocation half actually delivers it. Confirmed via agy's own
# builtin/skills/agy-customizations/docs/hooks.md (PostToolUse output
# contract: "Expects an empty JSON object {}"; PostInvocation output
# contract: supports injectSteps).
#
# KNOWN LIMITATION: agy's full built-in tool taxonomy could not be enumerated
# (see changelog/multi-harness-support.md) -- "run_command" is the only tool
# name confirmed from agy's own docs. Every other tool name is unknown, so
# this script cannot maintain a skip-list for agy's read-only/navigation
# tools the way the claude/codex versions do for Read/Glob/view_image/etc.
# Only run_command is treated as substantive here; every other tool name is
# currently ignored (treated as non-substantive) rather than risk nagging on
# an unconfirmed read-only tool. Revisit if agy's tool names ever become
# enumerable.
set -euo pipefail

input=$(cat)
tool_name=$(echo "$input" | jq -r '.toolCall.name // empty')

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"
flag_file="${tickets_dir}/.antigravity_missing_step_flag"

if [[ "$tool_name" != "run_command" ]]; then
    echo '{}'
    exit 0
fi

# Skip if the command itself is invoking tk (creating/managing steps).
# Regex match (not a case/glob) -- same fix and same reasoning as
# scripts/codex_require_steps_pretool.sh (see that file's comment): the old
# glob's catch-all `*tk\ *` alternative false-negatived on any command
# merely containing "tk " (e.g. `apt-get install -y python3-tk`), and had
# no bare `ticket ...` alternative.
command=$(echo "$input" | jq -r '.toolCall.args.CommandLine // empty')
if [[ "$command" =~ (^|/|[[:space:]])(tk|ticket)[[:space:]] ]]; then
    echo '{}'
    exit 0
fi

if [[ ! -d "$tickets_dir" ]]; then
    echo '{}'
    exit 0
fi

tk_script="${repo_root}/vendor/tk/ticket"
if [[ ! -x "$tk_script" ]]; then
    echo '{}'
    exit 0
fi

export TICKETS_DIR="$tickets_dir"

in_progress=$("$tk_script" steps --status=in_progress 2>/dev/null | sed '/^[[:space:]]*$/d' || true)
if [[ -n "$in_progress" ]]; then
    rm -f "$flag_file"
    echo '{}'
    exit 0
fi

open_steps=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' || true)
if [[ -n "$open_steps" ]]; then
    echo "no_in_progress" > "$flag_file"
else
    echo "no_steps" > "$flag_file"
fi

echo '{}'
