#!/usr/bin/env bash
# UserPromptSubmit hook: recurring reminder to persist anything worth
# remembering from the previous turn to the shared memory MCP server.
# Fires at the start of every new turn -- Stop can't deliver a
# model-visible reminder without blocking (its only content channel is
# exit 2, forcing the agent to continue -- too strong for an advisory
# nudge), confirmed via claude_open_tickets_stop_nudge.sh's own comment
# ("stderr message is mainly for orchestrator log / human visibility").
# UserPromptSubmit is the real mechanism, mirroring the tk-steps
# UserPromptSubmit reminder (claude_open_tickets_reminder.sh) exactly.
set -euo pipefail

cat > /dev/null 2>&1 || true

jq -n --arg ctx "
[Memory reminder]

If your previous turn surfaced any fact, decision, or piece of context worth remembering across sessions, persist it now via the shared memory MCP server (create_entities / add_observations) before moving on.

See CLAUDE.md > Memory for the full protocol.
" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
