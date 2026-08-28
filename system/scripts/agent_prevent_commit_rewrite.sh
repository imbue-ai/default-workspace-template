#!/usr/bin/env bash
set -euo pipefail

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

# Check if command was extracted
if [[ -z "$command" ]]; then
    echo "No command found in input" >&2
    exit 0
fi

# Check if command starts with "git rebase"
if [[ "$command" =~ ^git[[:space:]]+rebase ]]; then
    echo "Blocked: git rebase commands are not allowed" >&2
    exit 2
fi

# Check if command is "git pull" with --rebase or -r flag
if [[ "$command" =~ ^git[[:space:]]+pull ]]; then
    if [[ "$command" == *"--rebase"* ]] || [[ "$command" =~ (^|[[:space:]])-r([[:space:]]|$) ]]; then
        echo "Blocked: git pull --rebase commands are not allowed (use git pull --merge instead)" >&2
        exit 2
    fi
fi

# Check if command starts with "git commit" and contains --amend or --fixup
if [[ "$command" =~ ^git[[:space:]]+commit ]]; then
    if [[ "$command" == *"--amend"* ]] || [[ "$command" == *"--fixup"* ]]; then
        echo "Blocked: git commit with --amend or --fixup is not allowed" >&2
        exit 2
    fi
fi

# Command is allowed
exit 0
