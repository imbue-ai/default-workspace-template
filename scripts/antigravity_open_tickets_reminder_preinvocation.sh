#!/usr/bin/env bash
# PreInvocation hook (antigravity): substitute for claude_open_tickets_reminder.sh's
# UserPromptSubmit. antigravity has no UserPromptSubmit-equivalent event --
# PreInvocation is the closest real substitute, but it fires before every
# model call within a turn (as tool calls proceed), not exclusively at
# fresh user-message submission -- coarser than claude's exactly-once
# semantics. Unlike hook #4's antigravity port, this needs only one hook
# (PreInvocation alone has both "runs before the model" and "can inject
# text" in one place -- no PostToolUse/PostInvocation state-file
# composition required here).
#
# Accepted tradeoff, same class as hook #4/#5's other approximations: the
# reminder may repeat mid-turn rather than firing exactly once per user
# message. Since the reminder is idempotent/advisory (re-reading it does no
# harm, unlike a hard block), this is judged acceptable rather than adding
# session-keyed state to suppress repeats.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

cat > /dev/null

[[ -d "$tickets_dir" ]] || { echo '{}'; exit 0; }

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || { echo '{}'; exit 0; }

export TICKETS_DIR="$tickets_dir"

open_lines=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

if [[ -z "$open_lines" ]]; then
    echo '{}'
    exit 0
fi

msg=$(cat <<EOF
[Open task reminder from forever-claude-template]

You have step records that are not yet closed:

$open_lines

For each one, decide before continuing: keep working on it (call \`tk start <id>\` if it is not already in_progress), replace it with a fresh step, or close it now with \`tk close <id> "<summary>"\` (the positional summary is required for steps). Steps are sequential: do not start a new step until the previous one is closed.

See AGENTS.md > Task management for the full protocol.
EOF
)

jq -n --arg msg "$msg" '{injectSteps: [{ephemeralMessage: $msg}]}'
