#!/usr/bin/env bash
# UserPromptSubmit hook: while the workspace is short of disk, memory or CPU,
# tell the agent so, so it does not add to the problem (a large download into
# a nearly full disk, a test suite into nearly full memory) and can tell the
# user. Silent otherwise.
#
# The shell's resource monitor writes the notice file while a warning stands
# and rewrites it on every reading (every few seconds), removing it when the
# warning clears. A file that has not been touched for two minutes was left by
# a shell that is no longer running, and is ignored.
#
# Output channel: as agent_open_tickets_reminder.sh -- plain stdout for claude,
# `--codex` for the JSON form codex needs.
set -euo pipefail

emit_as_codex=0
[[ "${1:-}" == "--codex" ]] && emit_as_codex=1

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
notice_file="${RESOURCE_AGENT_NOTICE_FILE:-${repo_root}/data/.state/system_interface/resource_agent_notice.txt}"

# Drain stdin.
cat > /dev/null

[[ -s "$notice_file" ]] || exit 0
[[ -n "$(find "$notice_file" -mmin -2 2>/dev/null)" ]] || exit 0

notice=$(cat "$notice_file")

if [[ "$emit_as_codex" -eq 1 ]]; then
    jq -n --arg ctx "$notice" \
        '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
else
    printf '%s\n' "$notice"
fi
