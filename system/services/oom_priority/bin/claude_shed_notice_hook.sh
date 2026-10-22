#!/usr/bin/env bash
# SessionStart hook: the shell front of claude_shed_notice_hook.py.
#
# A session's first message waits for every SessionStart hook, and starting Python
# is the slowest thing this hook does. It has something to say only when the shed
# ledger names this agent, so a grep for the name decides whether Python runs at
# all; it can match more than it should, and Python still decides what is pending.
#
# The ledger path mirrors oom_priority.paths.shed_ledger_path.
set -euo pipefail

agent_name="${MNGR_AGENT_NAME:-}"
runtime_dir="${OOM_PRIORITY_RUNTIME_DIR:-${MNGR_AGENT_WORK_DIR:-$PWD}/runtime/oom_priority}"
ledger="$runtime_dir/events/shed.jsonl"

[[ -n "$agent_name" && -f "$ledger" ]] || exit 0
grep -qF "\"$agent_name\"" "$ledger" || exit 0
python3 "$(dirname "${BASH_SOURCE[0]}")/claude_shed_notice_hook.py" 2>/dev/null || true
