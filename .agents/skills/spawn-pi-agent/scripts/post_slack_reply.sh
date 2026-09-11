#!/usr/bin/env bash
set -euo pipefail

CHANNEL=""
THREAD_TS=""
BODY=""
LINK=""
AGENT_ID=""
HOSTNAME=""

usage() {
  cat <<EOF
Usage: $0 --channel <id> --thread-ts <ts> --body <text> --link <url> [--agent-id <id>] [--hostname <name>]

Post a short Slack thread reply signed as a mind agent.
The body + link together must be <= 160 characters.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) CHANNEL="$2"; shift 2 ;;
    --thread-ts) THREAD_TS="$2"; shift 2 ;;
    --body) BODY="$2"; shift 2 ;;
    --link) LINK="$2"; shift 2 ;;
    --agent-id) AGENT_ID="$2"; shift 2 ;;
    --hostname) HOSTNAME="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -z "$CHANNEL" || -z "$THREAD_TS" || -z "$BODY" || -z "$LINK" ]] && usage

# Fallback agent-id / hostname from mngr list if not provided.
if [[ -z "$AGENT_ID" || -z "$HOSTNAME" ]]; then
  THIS_AGENT="${MNGR_AGENT_NAME:-}"
  if [[ -z "$THIS_AGENT" ]]; then
    # If we know our own agent id, use that; otherwise leave blank.
    THIS_AGENT="${MNGR_AGENT_ID:-}"
  fi
  if [[ -n "$THIS_AGENT" ]]; then
    RECORD=$(mngr list --format jsonl --on-error continue 2>/dev/null | jq -r --arg name "$THIS_AGENT" 'select(.name == $name and .resource_type == "agent")' || true)
    if [[ -z "$AGENT_ID" ]]; then
      AGENT_ID=$(echo "$RECORD" | jq -r '.id // empty')
    fi
    if [[ -z "$HOSTNAME" ]]; then
      HOSTNAME=$(echo "$RECORD" | jq -r '.host.name // empty')
    fi
  fi
fi

SIGNOFF="sent from :brain:"
if [[ -n "$AGENT_ID" && -n "$HOSTNAME" ]]; then
  SIGNOFF="${SIGNOFF}"$'\n'"[mind:${AGENT_ID}@${HOSTNAME}]"
fi

COMBINED="${BODY}"$'\n'"${LINK}"
if [[ ${#COMBINED} -gt 160 ]]; then
  echo "Warning: body + link is ${#COMBINED} characters (>160). Slack criteria may fail." >&2
fi

TEXT="${COMBINED}"$'\n'"${SIGNOFF}"

PAYLOAD=$(jq -n \
  --arg channel "$CHANNEL" \
  --arg thread_ts "$THREAD_TS" \
  --arg text "$TEXT" \
  '{channel: $channel, thread_ts: $thread_ts, text: $text, unfurl_links: false}')

echo "Posting Slack reply to channel=$CHANNEL thread_ts=$THREAD_TS"
latchkey curl -X POST https://slack.com/api/chat.postMessage \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD"
echo