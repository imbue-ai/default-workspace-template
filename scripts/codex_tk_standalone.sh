#!/usr/bin/env bash
# PreToolUse hook (codex): direct port of claude_tk_standalone.sh. Same
# checker script (claude_tk_standalone_check.py -- pure shlex parsing,
# already agent-agnostic, unmodified), same exit-2 + stderr contract
# confirmed valid for codex's PreToolUse. Tool-name check covers both
# "Bash" and "shell" defensively (see codex_require_steps_pretool.sh's
# header comment for why -- the hook-facing name was not independently
# reconfirmed live).
set -euo pipefail

input=$(cat)

tool_name=$(echo "$input" | jq -r '.tool_name // empty')
[[ "$tool_name" == "Bash" || "$tool_name" == "shell" ]] || exit 0

command=$(echo "$input" | jq -r '.tool_input.command // empty')
[[ -n "$command" ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 "$script_dir/claude_tk_standalone_check.py" "$command"
