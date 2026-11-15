#!/usr/bin/env bash
set -euo pipefail

# Block a foreground `sleep` of 5 seconds or more: the agent should wait on the event it cares
# about (a background command finishing, a state it can check) rather than on a guess at how
# long that takes. The decision lives in workspace_bare_scripts/agent_block_foreground_sleep_check.py.

input=$(cat)

tool_name=$(echo "$input" | jq -r '.tool_name // empty')
if [[ -n "$tool_name" && "$tool_name" != "Bash" ]]; then
    exit 0
fi

# claude's own background option: the sleep holds no turn, and the task's end wakes the agent.
if [[ "$(echo "$input" | jq -r '.tool_input.run_in_background // false')" == "true" ]]; then
    exit 0
fi

command=$(echo "$input" | jq -r '.tool_input.command // empty')
if [[ -z "$command" ]]; then
    exit 0
fi

# Cheap guard, matched with bash's own regex so it forks nothing: only a command whose text has
# `sleep` followed by a number or `inf`, quoted or not, pays for the Python checker.
sleep_re="(^|[^[:alnum:]_.-])sleep[[:space:]]+[\"']?(\\.?[0-9]|[iI][nN][fF])"
[[ "$command" =~ $sleep_re ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 "$script_dir/agent_hook_check.py" block_foreground_sleep "$command"
