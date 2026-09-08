#!/usr/bin/env bash
# Stop hook: NON-BLOCKING reminder if the agent stops while tickets are still
# open or in_progress. Exits 0 always so the agent is never re-engaged --
# carryover into the next turn (driven by the UserPromptSubmit reminder)
# handles real follow-up. This stderr message is mainly for orchestrator log
# / human visibility.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
# Honor any externally-set TICKETS_DIR (the agent's env normally pins it
# via .mngr/settings.toml -- e.g. /home/user/workspace/data/.tickets -- so the tk
# tickets live under data/ with the rest of the workspace data). Fall back to tk's
# unset-default of <repo>/.tickets when nothing is set.
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

# Drain stdin.
cat > /dev/null

[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/system/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

# Re-export so tk picks up the resolved value even when this hook runs
# from outside the repo root (which would otherwise trigger tk's
# parent-walk and potentially land on a random ancestor).
export TICKETS_DIR="$tickets_dir"

# `tk steps` lists this agent's open step records only (regular tickets
# are managed cross-agent and aren't part of the per-turn progress flow
# the chat view renders). The trailing `|| true` is load-bearing: `tk steps`
# exits non-zero when there are no steps at all, which under `set -euo pipefail`
# would abort this script with that non-zero code -- breaking the "exits 0
# always" contract (and, on codex, an exit 2 on Stop is read as a continuation
# request, re-engaging the agent). `|| true` keeps the count at 0 and the exit
# clean. Mirrors the same guard in agent_open_tickets_reminder.sh.
open_count=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ' || true)

if [[ "${open_count:-0}" -gt 0 ]]; then
    echo "[task-management] Stopping with ${open_count} step record(s) still open. They'll appear at the top of the next turn's progress block." >&2
fi
exit 0
