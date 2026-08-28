#!/usr/bin/env bash
set -euo pipefail

# Block commands that pipe through tail or head.
# Instead, the agent should run the command redirected to a file and then read from it.

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

# Check if the command pipes through tail or head (e.g. "| tail -20", "| head -5")
# Match: pipe followed by optional whitespace, then tail or head, optionally with args
if echo "$command" | grep -qE '\|\s*(tail|head)(\s|$)'; then
    echo "Do not pipe commands through tail or head. Instead, redirect output to a temp file (e.g. cmd > /tmp/output.txt) and then read from that file separately using the Read tool or a separate tail/head command on the file." >&2
    exit 2
fi

exit 0
