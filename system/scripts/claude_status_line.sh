#!/usr/bin/env bash
set -euo pipefail
# Status line script for Claude Code
# Outputs: [time user@host dir] branch | PR: url (status)
#
# Side effect: records the session's live model state for the chat model bar.
# See docs/system/blueprint/live-model-state/plan-live-model-state.md.

# Claude Code pipes a JSON payload on stdin (model.id, effort.level, fast_mode,
# session_id, ...). Consume it once; it drives the model-state write below.
PAYLOAD=$(cat 2>/dev/null || true)

# Write the unified live model state that the system interface's model bar
# reads. Guards: only inside an mngr agent (MNGR_AGENT_STATE_DIR set), and only
# for the agent's MAIN session -- a nested interactive claude in the same pane
# environment would otherwise fight over the file every refresh tick. The main
# session's id is recorded by mngr's SessionStart hook; before that first write
# the ids mismatch and we skip once, corrected on the next fire.
if [[ -n "${MNGR_AGENT_STATE_DIR:-}" && -n "$PAYLOAD" ]]; then
    RECORDED_SID=$(cat "$MNGR_AGENT_STATE_DIR/claude_session_id" 2>/dev/null || true)
    PAYLOAD_SID=$(jq -r '.session_id // empty' <<<"$PAYLOAD" 2>/dev/null || true)
    if [[ -n "$PAYLOAD_SID" && "$PAYLOAD_SID" == "$RECORDED_SID" ]]; then
        # select() drops payloads with no model id, yielding empty output.
        STATE=$(jq -c 'select(.model.id != null and .model.id != "")
            | {model: .model.id, effort: (.effort.level // null), fast: (.fast_mode == true)}' \
            <<<"$PAYLOAD" 2>/dev/null || true)
        if [[ -n "$STATE" ]]; then
            # Fixed tmp name: Claude Code cancels in-flight statusline scripts,
            # and a fixed name self-heals orphaned tmp files on the next fire.
            TMP="$MNGR_AGENT_STATE_DIR/minds_model_state.json.tmp"
            if printf '%s' "$STATE" > "$TMP" 2>/dev/null; then
                mv -f "$TMP" "$MNGR_AGENT_STATE_DIR/minds_model_state.json" 2>/dev/null || true
            fi
        fi
    fi
fi

# Get basic info
TIME=$(date +%H:%M:%S)
USER=$(whoami)
HOST=$(hostname -s)
DIR=$(pwd)

# Get current git branch (single git spawn; empty outside a repo)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

# Get PR URL from .claude/pr_url (if exists)
PR_URL=""
if [[ -f "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_url" ]]; then
    PR_URL=$(cat "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_url" 2>/dev/null || echo "")
fi

# Get PR status from .claude/pr_status (if exists)
PR_STATUS=""
if [[ -f "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_status" ]]; then
    PR_STATUS=$(cat "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_status" 2>/dev/null || echo "")
fi

# Build the status line
STATUS_LINE="[$TIME $USER@$HOST $DIR]"

# Add branch info
if [[ -n "$BRANCH" ]]; then
    STATUS_LINE="$STATUS_LINE $BRANCH"
fi

# Add PR info if available
if [[ -n "$PR_URL" ]]; then
    if [[ -n "$PR_STATUS" ]]; then
        STATUS_LINE="$STATUS_LINE | PR: $PR_URL ($PR_STATUS)"
    else
        STATUS_LINE="$STATUS_LINE | PR: $PR_URL"
    fi
fi

printf '%s' "$STATUS_LINE"
