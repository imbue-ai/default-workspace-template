#!/usr/bin/env bash
# PreToolUse hook (antigravity): port of claude_prevent_commit_rewrite.sh.
# Unlike codex, agy's PreToolUse contract only documents a JSON
# {"decision": ...} output -- no confirmed bare exit-code fallback -- so
# this wraps the same rewrite-detection logic in that JSON shape instead of
# reusing claude's exit-2 pattern directly. Registered in .agents/hooks.json
# with matcher "run_command" so it only fires for that tool.
set -euo pipefail

deny() {
    jq -n --arg reason "$1" '{decision: "deny", reason: $reason}'
    exit 0
}

input=$(cat)

command=$(echo "$input" | jq -r '.toolCall.args.CommandLine // empty')

[[ -n "$command" ]] || exit 0

# Matches "git <verb>" at the start of the command OR right after a shell
# chain operator (&&, ;, |) -- a bare ^git anchor is trivially bypassed by
# `git add -A && git commit --amend` (found by code review). Same fix
# applied to the claude original and the codex port.
_CHAIN_ANCHOR='(^|&&|;|\|)[[:space:]]*'

if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"rebase" ]]; then
    deny "git rebase commands are not allowed"
fi

if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"pull" ]]; then
    if [[ "$command" == *"--rebase"* ]] || [[ "$command" =~ (^|[[:space:]])-r([[:space:]]|$) ]]; then
        deny "git pull --rebase commands are not allowed (use git pull --merge instead)"
    fi
fi

if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"commit" ]]; then
    if [[ "$command" == *"--amend"* ]] || [[ "$command" == *"--fixup"* ]]; then
        deny "git commit with --amend or --fixup is not allowed"
    fi
fi

exit 0
