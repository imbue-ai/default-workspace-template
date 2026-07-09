#!/usr/bin/env bash
# SessionStart hook (codex): port of claude_memory_reminder_sessionstart.sh.
# codex's SessionStart uses the same hookSpecificOutput.additionalContext
# mechanism as claude (confirmed via developers.openai.com/codex/hooks).
set -euo pipefail

cat > /dev/null 2>&1 || true

jq -n --arg ctx "
[Memory reminder]

Before starting work, search the shared memory MCP server (.mcp.json) for context relevant to this task -- call its search_nodes tool. Facts and decisions saved there may be relevant regardless of which harness saved them.

See AGENTS.md > Memory for the full protocol.
" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
