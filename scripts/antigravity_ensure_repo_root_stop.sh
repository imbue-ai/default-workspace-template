#!/usr/bin/env bash
# Stop hook (antigravity): port of the inline .git-existence check in
# claude's Stop hook array (.claude/settings.json) -- found missing during
# the multi-harness functionality-matrix audit, never ported alongside the
# other Stop hooks. Other Stop hooks assume they run from the repo root;
# this blocks the stop (forcing the agent back) if the agent wandered
# elsewhere. Omits claude's MNGR_CLAUDE_SUBAGENT_PROXY_CHILD guard --
# that's dead code even for claude, since disable_plugin__extend disables
# the claude_subagent_proxy plugin entirely, so the env var it checks is
# never set (same reasoning already applied when porting
# antigravity_tk_standalone.sh from claude_tk_standalone.sh).
set -euo pipefail

cat > /dev/null 2>&1 || true

if [[ ! -e .git ]]; then
    jq -n '{decision: "continue", reason: "Be sure to return to the repo root when you finish! Otherwise the other stop hooks cannot run correctly."}'
else
    echo '{}'
fi
