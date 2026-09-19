#!/usr/bin/env bash
# PreToolUse hook: HARD-BLOCK a tool call that reads or writes a file under
# data/.secrets/ directly (policy P8 in
# system/apps/chat/imbue/chat/harnesses/core-contracts/tool-call-policies.md).
#
# A secret file holds a value the user gave the chat app through a secret card so
# that it would never enter the transcript. The only sanctioned reader is
# system/scripts/with_secrets.py, which puts the file's variables into a child
# process's environment; `ls` and `rm` on the directory are allowed, and so is the
# request script, whose output names the path it will write. Everything else that
# names the directory -- `cat`, `source`, `sed`, `python3 -c`, a redirect, a Read
# or Edit tool call on a file there -- is refused with the reason on stderr.
#
# Unlike the shell-only guards, this one also polices the file tools, because a
# `Read` of the file defeats the rule as surely as a `cat`. The whole hook payload
# goes to the checker, which knows which tool names carry a command and which
# carry a path (claude's, codex's, and pi's spellings).
#
# Blocks via exit 2 with a stderr message the agent sees. The parsing lives in the
# sibling agent_secrets_guard_check.py, which shell-tokenizes with `shlex` so a
# quoted rationale that mentions the directory stays inside one token, and which
# never prints the command or the path back (either may carry a value).
set -euo pipefail

input=$(cat)

# Cheap guard: every refused call names the directory somewhere in its payload, so
# the overwhelming majority of calls never pay for the checker.
[[ "$input" == *".secrets"* ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 "$script_dir/agent_secrets_guard_check.py" <<<"$input"
