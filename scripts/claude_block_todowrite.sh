#!/usr/bin/env bash
# PreToolUse hook for TodoWrite: deny with a message redirecting the agent
# to `tk create --step` for per-turn progress tracking. The chat UI
# renders task progress from tk step records, so dual-tracking with
# TodoWrite would split the source of truth. CLAUDE.md "Task management"
# describes the protocol.
set -euo pipefail

# Drain stdin (we ignore it; we always deny TodoWrite calls).
cat > /dev/null

jq -n '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "TodoWrite is disabled in this project. Use `tk create --step` for per-turn progress tracking -- it fills the same role TodoWrite would (declaring plan steps and tracking completion), but the chat UI renders progress from tk step records, not TodoWrite. Lifecycle: `tk create --step \"...\"` -> `tk start <id>` -> `tk close <id> \"summary\"`. See CLAUDE.md > Task management."
  }
}'
