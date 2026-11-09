#!/usr/bin/env bash
# PreToolUse and UserPromptSubmit hook: tell a chat's own agent, as hidden context, when a message
# from this chat is handed to the quick-change agent and when that agent has finished the change
# (system/apps/chat/imbue/chat/chat_quick_change.py routes it; the quick-change agent reports with
# system/scripts/report_quick_change.py). Without the notes the chat's agent does not know the live
# app files are changing under it, and its next edit of the same app can overwrite the change.
#
# The note rides hookSpecificOutput.additionalContext, which reaches the model and is not shown as
# a chat item. Each hand-over and report is passed on once per chat; the sibling
# agent_quick_change_notes.py does the reading and keeps what it has passed on under
# data/.state/quick_change_notes/.
#
# Runs before every tool call, so everything here is a cheap exit for the common case. Every
# failure exits 0 with no output: a missed note must never get in the way of the agent's work.
set -uo pipefail

input=$(cat)

# Claude's own subagents run in this checkout too; the note belongs to the chat's main agent.
[[ -z "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]] || exit 0

# The chat app sets MINDS_CHAT_ID on every agent it creates. launch-task workers and other agents
# created outside the chat app carry none, and no request is ever routed from them.
chat_id="${MINDS_CHAT_ID:-}"
[[ "$chat_id" =~ ^[A-Za-z0-9_.-]+$ ]] || exit 0

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
chat_data_dir="${repo_root}/data/.apps/chat"
log="${chat_data_dir}/quick_changes.jsonl"
[[ -f "$log" ]] || exit 0

# The quick-change agents are chats the chat app created too, one per tier; their ids are recorded
# beside the log, keyed by tier ({"fast": {"chat_id": ...}, "careful": {...}}), or as one top-level
# chat_id in a record from before tiers. A quoted match anywhere in the file covers both shapes.
agent_record="${chat_data_dir}/quick_change_agent.json"
if [[ -f "$agent_record" ]] && [[ "$(<"$agent_record")" == *"\"${chat_id}\""* ]]; then
    exit 0
fi

# The log is append-only, so an unchanged size means no new hand-over or report. The state file's
# first line is the log size when it was last read.
state="${repo_root}/data/.state/quick_change_notes/${chat_id}"
log_size=$(wc -c <"$log" 2>/dev/null) || exit 0
# BSD wc pads the count with spaces.
log_size="${log_size//[[:space:]]/}"
seen_size=""
[[ -f "$state" ]] && read -r seen_size <"$state"
[[ "$log_size" != "$seen_size" ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
python3 "$script_dir/agent_quick_change_notes.py" --chat-id "$chat_id" --log "$log" --state "$state" \
    <<<"$input" 2>/dev/null || true
exit 0
