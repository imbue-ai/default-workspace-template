#!/usr/bin/env bash
# Tells the AI Usage Limits app that this chat just did something (the user sent a
# message, the agent used a tool, or the agent finished its turn), so the app can
# take a usage reading and credit any increase to the chats that were working.
# Never blocks the chat and never fails it: the post runs detached and errors are dropped.
set -euo pipefail
event="${1:-activity}"
chat_id="${MINDS_CHAT_ID:-${MNGR_AGENT_ID:-}}"
[ -n "$chat_id" ] || exit 0
payload=""
# Only the message and finish events carry their hook input (the message text labels
# the turn); tool events can carry large tool output, which the app does not need.
if [ "$event" = "prompt" ] || [ "$event" = "stop" ]; then
  payload="$(head -c 20000)"
else
  cat >/dev/null
fi
(
  curl -s -m 3 -o /dev/null -X POST "http://127.0.0.1:${AI_USAGE_PORT:-8080}/api/activity" \
    -H "Content-Type: application/json" \
    -H "X-Chat-Id: $chat_id" \
    -H "X-Event: $event" \
    --data-binary "${payload:-{\}}" >/dev/null 2>&1 &
)
exit 0
