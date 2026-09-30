#!/usr/bin/env bash
set -euo pipefail
# Status line script for Claude Code. It prints nothing, so the status line row
# stays blank; its job is recording the session's live model state for the chat
# model bar. See docs/system/blueprint/live-model-state/plan-live-model-state.md.
#
# .claude/settings.json re-runs it every refreshInterval seconds in every claude
# session, so it uses shell builtins and spawns at most one jq per run.

# Claude Code pipes a JSON payload on stdin (model.id, effort.level, fast_mode,
# session_id, ...). Consume it whether or not it is used.
PAYLOAD=""
IFS= read -r -d '' PAYLOAD || true

# Only inside an mngr agent (MNGR_AGENT_STATE_DIR set), and only for the
# agent's MAIN session -- a nested interactive claude in the same pane
# environment would otherwise fight over the file every refresh tick. The main
# session's id is recorded by mngr's SessionStart hook; before that first write
# there is nothing to match and we skip, corrected on the next fire.
if [[ -z "${MNGR_AGENT_STATE_DIR:-}" || -z "$PAYLOAD" ]]; then
    exit 0
fi
RECORDED_SID=""
if [[ -f "$MNGR_AGENT_STATE_DIR/claude_session_id" ]]; then
    read -r RECORDED_SID < "$MNGR_AGENT_STATE_DIR/claude_session_id" || true
fi
if [[ -z "$RECORDED_SID" ]]; then
    exit 0
fi

# select() drops payloads from another session or with no model id, yielding
# empty output.
STATE=$(jq -c --arg sid "$RECORDED_SID" 'select(.session_id == $sid and .model.id != null and .model.id != "")
    | {model: .model.id, effort: (.effort.level // null), fast: (.fast_mode == true)}' \
    <<<"$PAYLOAD" 2>/dev/null) || true
if [[ -z "$STATE" ]]; then
    exit 0
fi

# Rewrite only on a change: the chat app reacts to every rewrite of this file.
MODEL_STATE_FILE="$MNGR_AGENT_STATE_DIR/model_state.json"
CURRENT_STATE=""
if [[ -f "$MODEL_STATE_FILE" ]]; then
    read -r CURRENT_STATE < "$MODEL_STATE_FILE" || true
fi
if [[ "$STATE" == "$CURRENT_STATE" ]]; then
    exit 0
fi

# Fixed tmp name: Claude Code cancels in-flight statusline scripts, and a fixed
# name self-heals orphaned tmp files on the next fire.
TMP="$MNGR_AGENT_STATE_DIR/model_state.json.tmp"
if printf '%s' "$STATE" > "$TMP" 2>/dev/null; then
    mv -f "$TMP" "$MODEL_STATE_FILE" 2>/dev/null || true
fi
