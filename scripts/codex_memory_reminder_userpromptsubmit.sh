#!/usr/bin/env bash
# UserPromptSubmit hook (codex): port of
# claude_memory_reminder_userpromptsubmit.sh. codex accepts the same
# hookSpecificOutput.additionalContext mechanism for UserPromptSubmit as
# claude (confirmed via developers.openai.com/codex/hooks).
set -euo pipefail

cat > /dev/null 2>&1 || true

jq -n --arg ctx "
[Memory reminder]

If your previous turn surfaced any fact, decision, or piece of context worth remembering across sessions, persist it now via the shared memory MCP server (create_entities / add_observations) before moving on.

See AGENTS.md > Memory for the full protocol.
" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
