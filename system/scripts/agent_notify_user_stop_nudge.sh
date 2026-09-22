#!/usr/bin/env bash
# Stop hook: ask a chat agent, at the end of every turn, to tell the user it
# finished -- the notification (bell, badge, toast, and a system banner when
# they are looking elsewhere) is the only thing that reaches a user who walked
# away, and clicking it lands them back in this chat.
#
# A suggestion, not a gate. Whether a given turn is worth a notification is a
# judgement only the agent can make, so the message offers an explicit way out:
# say nothing and stop. That costs one short continuation on a turn that does
# not want one, which is the price of not guessing from the outside.
#
# The reminder has to exit 2 to reach the model at all -- a Stop hook's stdout
# and its stderr on exit 0 go to the debug log, which is why the open-steps stop
# nudge beside this one is decorative. Exit 2 makes claude continue the
# conversation with this stderr as the message; `stop_hook_active` on the next
# Stop payload is how claude says "you are already continuing because of a stop
# hook", and is the documented way to let that continuation through rather than
# block forever (https://code.claude.com/docs/en/hooks). It is also the only
# state this needs: no turn markers, no bookkeeping files.
#
# Chat agents only (MNGR_AGENT_ROLE=chat, set by the `chat` create template):
# a worker's results reach the user through the chat that launched it, and only
# chats appear in the app's notification feed.
#
# claude only. codex acts on an exit 2 at Stop (it turns it into a new prompt),
# but nothing measured says whether it sends a `stop_hook_active` equivalent,
# and an exit 2 with no way to recognise the continuation is an endless loop in
# a live chat. See tool-call-policies-state-of-things.md, P8.
set -euo pipefail

input=$(cat)

[[ -z "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]] || exit 0
[[ "${MNGR_AGENT_ROLE:-}" == "chat" ]] || exit 0

if [[ "$(echo "$input" | jq -r '.stop_hook_active // false')" == "true" ]]; then
    exit 0
fi

cat >&2 <<'EOF'
[Finish notification]

Tell the user this turn is done, so it reaches them even if they walked away from this chat:

  python3 .agents/skills/notify-user/scripts/notify_user.py "<one plain sentence saying what is now done>"

One sentence in the user's terms -- what they can now see, use, or decide -- never the tool names or the steps. Read the exit code: when it is non-zero the notification did not go out, and your reply should say so. The `notify-user` skill has the full guidance.

If this turn does not warrant one -- chitchat, an acknowledgement, a trivial answer, a question you put to the user, a single quick read -- then output NOTHING at all in response to this message. Do not explain and do not acknowledge it: just stop.
EOF
exit 2
