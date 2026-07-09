#!/usr/bin/env bash
# Stop hook (codex): direct port of claude_open_tickets_stop_nudge.sh --
# identical logic, non-blocking (always exit 0), so no output-format
# translation is needed at all regardless of harness.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

cat > /dev/null

[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

export TICKETS_DIR="$tickets_dir"

open_count=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')

if [[ "${open_count:-0}" -gt 0 ]]; then
    echo "[task-management] Stopping with ${open_count} step record(s) still open. They'll appear at the top of the next turn's progress block." >&2
fi
exit 0
