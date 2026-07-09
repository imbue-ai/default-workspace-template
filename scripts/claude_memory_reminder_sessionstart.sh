#!/usr/bin/env bash
# SessionStart hook: reminds the agent to search the shared memory MCP
# server for relevant context before starting work. autoMemoryDirectory
# used to do this automatically (guaranteed, no model action needed); the
# shared MCP replacement requires the model to call search_nodes itself,
# with nothing enforcing it -- this is the mitigation (found by code
# review: "no hook anywhere enforcing the new tools actually get called").
# Advisory only, same as the tk-steps reminders -- there's no checkable
# state to gate on (unlike tk's open/in_progress steps), so this fires
# unconditionally once per fresh session rather than being conditional.
set -euo pipefail

cat > /dev/null 2>&1 || true

jq -n --arg ctx "
[Memory reminder]

Before starting work, search the shared memory MCP server (.mcp.json) for context relevant to this task -- call its search_nodes tool. Facts and decisions saved there may be relevant regardless of which harness saved them.

See CLAUDE.md > Memory for the full protocol.
" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
