#!/usr/bin/env bash
set -euo pipefail

AGENT_NAME=""
CHANNEL=""
THREAD_TS=""
TIMEOUT=3600
POLL_INTERVAL=15

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: $0 --agent-name <name> --slack-channel <id> --slack-thread-ts <ts> [--timeout <seconds>] [--poll-interval <seconds>]

Poll a pi agent's transcript until it looks done, then post a Slack reply.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-name) AGENT_NAME="$2"; shift 2 ;;
    --slack-channel) CHANNEL="$2"; shift 2 ;;
    --slack-thread-ts) THREAD_TS="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -z "$AGENT_NAME" || -z "$CHANNEL" || -z "$THREAD_TS" ]] && usage

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "/tmp/spawn-pi-agent-${AGENT_NAME}.log"
}

get_agent_state() {
  mngr list --format jsonl --on-error continue 2>/dev/null | \
    jq -r --arg name "$AGENT_NAME" 'select(.name == $name and .resource_type == "agent") | .state // "UNKNOWN"'
}

get_agent_record() {
  mngr list --format jsonl --on-error continue 2>/dev/null | \
    jq -r --arg name "$AGENT_NAME" 'select(.name == $name and .resource_type == "agent")'
}

get_latest_transcript() {
  mngr transcript "$AGENT_NAME" 2>/dev/null || true
}

extract_event_link() {
  local text="$1"
  # Google Calendar event URLs are the usual evidence for calendar tasks.
  echo "$text" | grep -Eo 'https://(www\.)?google\.com/calendar/event[^[:space:]<>"]+' | head -1
}

extract_last_agent_text() {
  local text="$1"
  # The common transcript prints "agent:" lines. Strip leading timestamps/brackets.
  echo "$text" | awk '/^\[/ && /agent:/{flag=1; next} flag{print}' | tail -40
}

looks_failed() {
  local text="$1"
  # Heuristic: agent says it failed or needs permission it can't get.
  echo "$text" | grep -Eiq 'failed|could not|unable to|permission denied|need user|stuck|give up' && return 0
  return 1
}

DEADLINE=$(($(date +%s) + TIMEOUT))
POSTED=0

log "Monitoring agent $AGENT_NAME for Slack thread $CHANNEL/$THREAD_TS"

while [[ $(date +%s) -lt $DEADLINE ]]; do
  STATE=$(get_agent_state)
  TRANSCRIPT=$(get_latest_transcript)
  LAST_TEXT=$(extract_last_agent_text "$TRANSCRIPT")

  EVENT_LINK=$(extract_event_link "$TRANSCRIPT")

  if [[ -n "$EVENT_LINK" ]]; then
    log "Found event link: $EVENT_LINK"
    # Compose a short body. Try to infer a title from the transcript.
    BODY="Done. Link below."
    if echo "$LAST_TEXT" | grep -iq 'fire alarm'; then
      BODY="Fire alarm/inspection Mon 9/14 8am-12pm."
    elif echo "$LAST_TEXT" | grep -iq 'calendar'; then
      BODY="Calendar notice added."
    fi
    "$SCRIPT_DIR/post_slack_reply.sh" \
      --channel "$CHANNEL" \
      --thread-ts "$THREAD_TS" \
      --body "$BODY" \
      --link "$EVENT_LINK" \
      --agent-id "$(get_agent_record | jq -r '.id // empty')" \
      --hostname "$(get_agent_record | jq -r '.host.name // empty')"
    POSTED=1
    break
  fi

  # If the agent ended its turn and sounds stuck/failed, post an update and stop.
  if [[ "$STATE" == "WAITING" || "$STATE" == "STOPPED" ]]; then
    if looks_failed "$LAST_TEXT"; then
      log "Agent appears failed/stuck."
      "$SCRIPT_DIR/post_slack_reply.sh" \
        --channel "$CHANNEL" \
        --thread-ts "$THREAD_TS" \
        --body "Could not complete the calendar update automatically." \
        --link "https://imbue-ai.slack.com/archives/${CHANNEL}/p${THREAD_TS//./}" \
        --agent-id "$(get_agent_record | jq -r '.id // empty')" \
        --hostname "$(get_agent_record | jq -r '.host.name // empty')" || true
      POSTED=1
      break
    fi
  fi

  sleep "$POLL_INTERVAL"
done

if [[ $POSTED -eq 0 ]]; then
  log "Timed out waiting for agent."
  "$SCRIPT_DIR/post_slack_reply.sh" \
    --channel "$CHANNEL" \
    --thread-ts "$THREAD_TS" \
    --body "Still working on the calendar update; will follow up." \
    --link "https://imbue-ai.slack.com/archives/${CHANNEL}/p${THREAD_TS//./}" \
    --agent-id "$(get_agent_record | jq -r '.id // empty')" \
    --hostname "$(get_agent_record | jq -r '.host.name // empty')" || true
fi

log "Monitor exiting."