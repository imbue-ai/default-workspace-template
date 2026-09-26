#!/usr/bin/env bash
set -euo pipefail

# Block commands that pipe output into tail or head when the rest of that output would be lost.
# Instead, the agent should run the command redirected to a file and then read from it. The
# decision lives in the sibling agent_block_pipe_tail_head_check.py.

# Read JSON input from stdin
input=$(cat)

# Only a SHELL call carries a command to police. codex renames its code-mode `exec_command` to
# `Bash`, but it ALSO delivers `apply_patch` through this same hook with the PATCH BODY in
# `.tool_input.command` -- so without this gate, editing any file whose contents contain a
# `| head` (a shell script, a README, the docs in this repo) is hard-blocked and the whole
# code-mode program aborts. Measured against codex-cli 0.147.0. claude/agy are unaffected:
# claude's edit tools carry `file_path`/`new_string` and never `command`, and agy's shim
# synthesises `{"tool_name":"Bash",...}`.
tool_name=$(echo "$input" | jq -r '.tool_name // empty')
if [[ -n "$tool_name" && "$tool_name" != "Bash" ]]; then
    exit 0
fi

# Extract the command from tool_input.command using jq
command=$(echo "$input" | jq -r '.tool_input.command // empty')

# Nothing to check
if [[ -z "$command" ]]; then
    exit 0
fi

# Cheap guard, matched with bash's own regex so it forks nothing: only a command whose text
# contains a pipe (`|` or `|&`, not `||`) into tail or head pays for the Python checker, which
# decides whether what feeds that pipe can be read again (a file read, git history) or not.
pipe_re='(^|[^|])\|&?[[:space:]]*(tail|head)([[:space:]]|$)'
[[ "$command" =~ $pipe_re ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 "$script_dir/agent_block_pipe_tail_head_check.py" "$command"
