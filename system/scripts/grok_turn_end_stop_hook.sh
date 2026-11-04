#!/usr/bin/env bash
# Runs the command in its arguments on a grok Stop hook's payload, only at a turn's end.
#
#   grok_turn_end_stop_hook.sh <command> [args...]
#
# grok fires Stop with `reason: "end_turn"` when a turn ends and with `reason: "shutdown"` when a
# session ends (at quit, `/new` or a resume), where a stop gate has no turn to check. A payload
# without a readable reason runs nothing.
set -euo pipefail

payload=$(cat)
reason=$(jq -r '.reason // empty' <<<"$payload" 2>/dev/null || true)
if [[ "$reason" != "end_turn" ]]; then
    exit 0
fi
exec "$@" <<<"$payload"
