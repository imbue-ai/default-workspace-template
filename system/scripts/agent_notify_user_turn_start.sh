#!/usr/bin/env bash
# UserPromptSubmit hook: open a turn for the finish-notification nudge.
#
# Records this chat's step records as they stand before the turn runs, and
# clears the markers the Stop hook reads, so each user message starts a fresh
# accounting: nothing notified yet, nothing nudged yet. Silent on every path --
# claude adds a UserPromptSubmit hook's stdout to the agent's context, and this
# one has nothing to say to the agent.
#
# Chat agents only (MNGR_AGENT_ROLE=chat, set by the `chat` create template):
# a worker's results reach the user through the chat that launched it, and only
# chats appear in the app's notification feed.
set -euo pipefail

# Drain stdin.
cat > /dev/null

[[ -z "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]] || exit 0
[[ "${MNGR_AGENT_ROLE:-}" == "chat" ]] || exit 0

source "${BASH_SOURCE[0]%/*}/_notify_user_turn_state.sh"

state_dir="$(notify_user_state_dir)"
mkdir -p "$state_dir"
rm -f "$state_dir/sent" "$state_dir/nudged"
notify_user_step_snapshot > "$state_dir/turn-steps"
exit 0
