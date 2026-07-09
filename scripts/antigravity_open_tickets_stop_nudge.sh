#!/usr/bin/env bash
# Stop hook (antigravity): port of claude_open_tickets_stop_nudge.sh.
# Non-blocking -- emits {} (no "decision" field, so the stop proceeds
# normally, mirroring PostToolUse's "expects an empty JSON object"
# convention) and logs to stderr for visibility only.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

cat > /dev/null

tk_script="${repo_root}/vendor/tk/ticket"

if [[ -d "$tickets_dir" && -x "$tk_script" ]]; then
    export TICKETS_DIR="$tickets_dir"
    open_count=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')
    if [[ "${open_count:-0}" -gt 0 ]]; then
        echo "[task-management] Stopping with ${open_count} step record(s) still open. They'll appear at the top of the next turn's progress block." >&2
    fi
fi

echo '{}'
