#!/usr/bin/env bash
set -euo pipefail

# Read JSON input from stdin
input=$(cat)

# Extract the command from tool_input.command using jq
command=$(echo "$input" | jq -r '.tool_input.command // empty')

# Check if command was extracted
if [[ -z "$command" ]]; then
    echo "No command found in input" >&2
    exit 0
fi

# Each check below matches "git <verb>" at the START of the command OR
# immediately after a shell chain operator (&&, ;, |) -- not just at the
# very start of the string. A bare `^git` anchor is trivially bypassed by
# `git add -A && git commit --amend` or `true; git pull -r` (found by code
# review: this exact gap let chained commands slip past undetected).
# _CHAIN_ANCHOR is not full shell parsing (nested subshells, command
# substitution, quoting are not modeled) -- it targets the realistic
# bypass named above, matching this hook's existing scope as a heuristic
# guard, not a hard security boundary.
_CHAIN_ANCHOR='(^|&&|;|\|)[[:space:]]*'

# Check if command runs "git rebase"
if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"rebase" ]]; then
    echo "Blocked: git rebase commands are not allowed" >&2
    exit 2
fi

# Check if command runs "git pull" with --rebase or -r flag
if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"pull" ]]; then
    if [[ "$command" == *"--rebase"* ]] || [[ "$command" =~ (^|[[:space:]])-r([[:space:]]|$) ]]; then
        echo "Blocked: git pull --rebase commands are not allowed (use git pull --merge instead)" >&2
        exit 2
    fi
fi

# Check if command runs "git commit" and contains --amend or --fixup
if [[ "$command" =~ $_CHAIN_ANCHOR"git"[[:space:]]+"commit" ]]; then
    if [[ "$command" == *"--amend"* ]] || [[ "$command" == *"--fixup"* ]]; then
        echo "Blocked: git commit with --amend or --fixup is not allowed" >&2
        exit 2
    fi
fi

# Command is allowed
exit 0
