#!/usr/bin/env bash
# Stop hook: a chat agent that did work this turn tells the user it is done.
#
# Exits 2 with the reminder on stderr, which both claude and codex hand back to
# the agent as a continuation -- so the agent can send the notification and then
# stop for real. It is a nudge, not a gate: the agent may decide a notification
# does not fit this turn, and stopping again goes through, because a nudge is
# spent on the step snapshot it fired for.
#
# Fires only when ALL of these hold:
#   - the agent is a chat (MNGR_AGENT_ROLE=chat, set by the `chat` create
#     template); a worker's results reach the user through its lead's chat, and
#     only chats appear in the app's notification feed;
#   - a turn is open (agent_notify_user_turn_start.sh ran on this prompt);
#   - the turn did work, i.e. it created, started or closed a step record --
#     chitchat, a clarifying question and a single quick read stay silent, the
#     same carve-out AGENTS.md makes for step records themselves;
#   - no notification has gone out this turn, and this snapshot has not already
#     been nudged for.
set -euo pipefail

# Drain stdin.
cat > /dev/null

[[ -z "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]] || exit 0
[[ "${MNGR_AGENT_ROLE:-}" == "chat" ]] || exit 0

source "${BASH_SOURCE[0]%/*}/_notify_user_turn_state.sh"

state_dir="$(notify_user_state_dir)"
[[ -f "$state_dir/turn-steps" ]] || exit 0
[[ ! -e "$state_dir/sent" ]] || exit 0

current_steps="$(notify_user_step_snapshot)"
[[ "$current_steps" != "$(cat "$state_dir/turn-steps")" ]] || exit 0
if [[ -f "$state_dir/nudged" && "$current_steps" == "$(cat "$state_dir/nudged")" ]]; then
    exit 0
fi

printf '%s' "$current_steps" > "$state_dir/nudged"

cat >&2 <<'EOF'
[Finish notification]

You did work this turn and have not told the user it is finished. Send the notification now, so it reaches them even if they walked away from this chat:

  python3 .agents/skills/notify-user/scripts/notify_user.py "<one plain sentence saying what is now done>"

One sentence in the user's terms -- what they can now see, use, or decide -- never the tool names or the steps. Read the exit code: when it is non-zero the notification did not go out, and your reply should say so.

See the `notify-user` skill for the full guidance. If a notification genuinely does not belong on this turn, skip it and finish -- this fires once for the work you have done.
EOF
exit 2
