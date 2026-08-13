#!/usr/bin/env bash
# Atlas live-tier reminder -- NON-BLOCKING. Wired to UserPromptSubmit. If the
# checkpoint clock has flagged that the agent did work (transcript turns) since a topic's last rich
# refresh, this prints a one-line nudge (added to the agent's context) so the
# working agent refreshes §1/§7 via `/atlas <slug> --live` at a free moment --
# the "agent upgrades when free" half of the hybrid (decision 1). Always exits 0.
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

atlas_hook_drain_stdin
if atlas_hook_is_subagent; then
    exit 0
fi

root="$(atlas_hook_root)"
engine="${root}/.agents/skills/atlas/scripts/atlas_checkpoint.py"
if ! atlas_hook_present "$engine" "${root}/atlas/topics"; then
    exit 0
fi

# stdout here is injected into the agent's context by UserPromptSubmit.
timeout 8 python3 "$engine" --reminder --repo-root "$root" 2>/dev/null || true
exit 0
