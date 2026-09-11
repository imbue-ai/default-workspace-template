#!/usr/bin/env bash
set -euo pipefail

NAME=""
TASK_FILE=""
PROVIDER="opencode-go"
MODEL="kimi-k2.7-code"
SLACK_CHANNEL=""
SLACK_THREAD_TS=""
MONITOR=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: $0 --name <agent-name> --task-file <path> [options]

Spawn a mngr pi agent with a chosen provider/model and optional Slack reply.

Options:
  --name              Agent name (required)
  --task-file         Markdown file with the initial task (required)
  --provider          Pi provider (default: opencode-go)
  --model             Pi model (default: kimi-k2.7-code)
  --slack-channel     Slack channel id for the reply
  --slack-thread-ts   Slack thread timestamp to reply to
  --monitor           Start a background monitor that posts the reply when done
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --task-file) TASK_FILE="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --slack-channel) SLACK_CHANNEL="$2"; shift 2 ;;
    --slack-thread-ts) SLACK_THREAD_TS="$2"; shift 2 ;;
    --monitor) MONITOR=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -z "$NAME" || -z "$TASK_FILE" ]] && usage
[[ -f "$TASK_FILE" ]] || { echo "Task file not found: $TASK_FILE" >&2; exit 1; }

echo "Spawning pi agent '$NAME' (provider=$PROVIDER model=$MODEL)..."
mngr create --message-file "$TASK_FILE" "$NAME" pi -- --provider "$PROVIDER" --model "$MODEL"

if [[ $MONITOR -eq 1 && -n "$SLACK_CHANNEL" && -n "$SLACK_THREAD_TS" ]]; then
  echo "Starting background monitor for Slack reply..."
  nohup "$SCRIPT_DIR/monitor_and_reply.sh" \
    --agent-name "$NAME" \
    --slack-channel "$SLACK_CHANNEL" \
    --slack-thread-ts "$SLACK_THREAD_TS" \
    >> "/tmp/spawn-pi-agent-${NAME}.log" 2>&1 &
  disown
  echo "Monitor PID $!. Log: /tmp/spawn-pi-agent-${NAME}.log"
else
  echo "Agent spawned. To post a Slack reply manually use:"
  echo "  $SCRIPT_DIR/post_slack_reply.sh --channel <id> --thread-ts <ts> --body \"...\" --link \"...\""
fi