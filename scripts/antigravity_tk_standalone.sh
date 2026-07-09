#!/usr/bin/env bash
# PreToolUse hook (antigravity): port of claude_tk_standalone.sh. Same
# checker script (claude_tk_standalone_check.py, unmodified), but its
# exit-code+stderr result is translated into agy's JSON decision shape
# (no confirmed bare exit-code fallback for agy's PreToolUse -- see
# antigravity_prevent_commit_rewrite.sh). Registered with matcher
# "run_command" in .agents/hooks.json.
set -euo pipefail

input=$(cat)

command=$(echo "$input" | jq -r '.toolCall.args.CommandLine // empty')
[[ -n "$command" ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
exit_code=0
reason=$(python3 "$script_dir/claude_tk_standalone_check.py" "$command" 2>&1 >/dev/null) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    jq -n --arg reason "$reason" '{decision: "deny", reason: $reason}'
fi

exit 0
