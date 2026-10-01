#!/usr/bin/env bash
set -euo pipefail
# Status line script for Claude Code. It prints nothing, so the status line row
# stays blank; its job is recording the session's live model state for the chat
# model bar. See docs/system/blueprint/live-model-state/plan-live-model-state.md.
#
# .claude/settings.json re-runs it every refreshInterval seconds in every claude
# session, so it uses shell builtins and spawns one jq per run, plus an mv only
# when the state changed.

# Only inside an mngr agent (MNGR_AGENT_STATE_DIR set), and only for the
# agent's MAIN session -- a nested interactive claude in the same pane
# environment would otherwise fight over the file every refresh tick. The main
# session's id is recorded by mngr's SessionStart hook; before that first write
# there is nothing to match and we skip, corrected on the next fire.
RECORDED_SID=""
if [[ -n "${MNGR_AGENT_STATE_DIR:-}" && -f "$MNGR_AGENT_STATE_DIR/claude_session_id" ]]; then
    read -r RECORDED_SID < "$MNGR_AGENT_STATE_DIR/claude_session_id" || true
fi

# Claude Code pipes a JSON payload on stdin (model.id, effort.level, fast_mode,
# session_id, ...). jq reads it directly: bash's read builtin would take it one
# byte per syscall. select() drops it when no main session is recorded, when it
# is from another session, or when it has no model id, yielding empty output.
STATE=$(jq -c --arg sid "$RECORDED_SID" 'select($sid != "" and .session_id == $sid
        and .model.id != null and .model.id != "")
    | {model: .model.id, effort: (.effort.level // null), fast: (.fast_mode == true)}' \
    2>/dev/null) || true
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
