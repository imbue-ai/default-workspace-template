#!/usr/bin/env bash
# Atlas checkpoint hook -- NON-BLOCKING. Wired to PostToolUse (arg: posttooluse)
# and Stop (arg: turn_end). Refreshes the §0 status line on the interval and logs
# a checkpoint event; it never calls a model, never commits, and always exits 0 so
# the agent that triggered it is never blocked or re-engaged.
# strict mode; guards use if/fi (not `&& exit`) so `-e` never trips on a false test.
set -euo pipefail

# Source the shared helpers; if they are absent (Atlas not installed here) no-op,
# so `set -e` never aborts on a missing source.
_atlas_common="${MNGR_AGENT_WORK_DIR:-$(pwd)}/.agents/skills/atlas/scripts/_atlas_hook_common.sh"
if [ ! -f "$_atlas_common" ]; then
    exit 0
fi
# shellcheck source=_atlas_hook_common.sh
. "$_atlas_common"

# Drain the hook's JSON stdin so the writer never blocks on a full pipe.
atlas_hook_drain_stdin

# Sub-agents / launch-task workers must not checkpoint (mirrors the Stop hook).
if atlas_hook_is_subagent; then
    exit 0
fi

reason="${1:-posttooluse}"
root="$(atlas_hook_root)"
engine="${root}/.agents/skills/atlas/scripts/atlas_checkpoint.py"

# Only act inside a workspace that actually has an Atlas book.
if ! atlas_hook_present "$engine" "${root}/atlas/topics"; then
    exit 0
fi

mkdir -p "${root}/data/.state/atlas" 2>/dev/null || true
# Bounded so a slow git call can never stall the agent; failures are swallowed.
timeout 8 python3 "$engine" --reason "$reason" --repo-root "$root" \
    >/dev/null 2>>"${root}/data/.state/atlas/hook.log" || true

exit 0
