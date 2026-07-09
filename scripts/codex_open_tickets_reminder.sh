#!/usr/bin/env bash
# UserPromptSubmit hook (codex): direct port of claude_open_tickets_reminder.sh.
# codex's UserPromptSubmit accepts plain stdout text as injected context,
# same as claude's (confirmed via developers.openai.com/codex/hooks: "Plain
# text on stdout is added as extra developer context") -- zero output-format
# translation needed.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

cat > /dev/null

[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

export TICKETS_DIR="$tickets_dir"

open_lines=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

[[ -n "$open_lines" ]] || exit 0

cat <<EOF

[Open task reminder from forever-claude-template]

You have step records that are not yet closed:

$open_lines

For each one, decide before continuing: keep working on it (call \`tk start <id>\` if it's not already in_progress), replace it with a fresh step, or close it now with \`tk close <id> "<summary>"\` (the positional summary is required for steps). The summary is a concise one-line description of the *work done* in this step (the caption a non-technical user sees), not the outcome -- the outcome goes in your final assistant message. Steps are sequential: do not start a new step until the previous one is closed.

See AGENTS.md > Task management for the full protocol.
EOF
