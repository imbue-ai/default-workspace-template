#!/usr/bin/env bash
# PreInvocation hook (antigravity): combined substitute for
# claude_memory_reminder_sessionstart.sh + claude_memory_reminder_userpromptsubmit.sh.
# antigravity has no SessionStart event, and PreInvocation fires before
# every model call within a turn (not exclusively at fresh user-message
# submission) -- same coarser-than-exact-once tradeoff already accepted for
# hook #5's antigravity port (antigravity_open_tickets_reminder_preinvocation.sh).
#
# The "search memory" text only fires once per agent lifetime (gated by a
# marker file, same pattern as antigravity_update_plugin_preinvocation.sh)
# since it's a true session-start concern. The "save to memory" text fires
# on every invocation, same recurring behavior as the tk-steps reminder.
set -euo pipefail

cat > /dev/null 2>&1 || true

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
marker="${repo_root}/runtime/.antigravity_memory_search_reminded"

save_msg="[Memory reminder]

If your previous turn surfaced any fact, decision, or piece of context worth remembering across sessions, persist it now via the shared memory MCP server (create_entities / add_observations) before moving on.

See AGENTS.md > Memory for the full protocol."

if [[ -f "$marker" ]]; then
    msg="$save_msg"
else
    mkdir -p "$(dirname "$marker")"
    touch "$marker"
    search_msg="[Memory reminder]

Before starting work, search the shared memory MCP server (mcp_config.json) for context relevant to this task -- call its search_nodes tool. Facts and decisions saved there may be relevant regardless of which harness saved them.

See AGENTS.md > Memory for the full protocol."
    msg="$search_msg"
fi

jq -n --arg msg "$msg" '{injectSteps: [{ephemeralMessage: $msg}]}'
