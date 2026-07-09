#!/usr/bin/env bash
# PreToolUse hook (codex): direct port of claude_prevent_commit_rewrite.sh.
# codex's PreToolUse accepts the same exit-2 + stderr deny mechanism as
# claude (confirmed via developers.openai.com/codex/hooks), and the input
# JSON shape (tool_name, tool_input.command) is confirmed identical -- zero
# logic changes needed beyond the shebang/file this lives in.
set -euo pipefail

input=$(cat)

command=$(echo "$input" | jq -r '.tool_input.command // empty')

if [[ -z "$command" ]]; then
    echo "No command found in input" >&2
    exit 0
fi

# Matches "git <verb>" at the start of the command OR right after a shell
# chain operator (&&, ;, |) -- a bare ^git anchor is trivially bypassed by
# `git add -A && git commit --amend` (found by code review). Same fix
# applied to the claude original and the antigravity port.
_CHAIN_ANCHOR='(^|&&|;|\|)[[:space:]]*'

if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"rebase" ]]; then
    echo "Blocked: git rebase commands are not allowed" >&2
    exit 2
fi

if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"pull" ]]; then
    if [[ "$command" == *"--rebase"* ]] || [[ "$command" =~ (^|[[:space:]])-r([[:space:]]|$) ]]; then
        echo "Blocked: git pull --rebase commands are not allowed (use git pull --merge instead)" >&2
        exit 2
    fi
fi

if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"commit" ]]; then
    if [[ "$command" == *"--amend"* ]] || [[ "$command" == *"--fixup"* ]]; then
        echo "Blocked: git commit with --amend or --fixup is not allowed" >&2
        exit 2
    fi
fi

exit 0
