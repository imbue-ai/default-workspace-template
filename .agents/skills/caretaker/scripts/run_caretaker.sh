#!/usr/bin/env bash
#
# run_caretaker.sh -- wake the singleton Caretaker agent for its nightly run.
#
# Invoked by the scheduler service (the `caretaker` task in
# runtime/scheduled_tasks.toml) once a night. Runs from the repo root
# (/mngr/code), in the services agent's environment (MNGR_HOST_DIR,
# MNGR_AGENT_ID, ... are inherited).
#
# The Caretaker is a singleton, identified by its `caretaker=true` label.
# Three branches:
#   - none exists          -> `mngr create` it (so it first appears on day 2)
#   - exists, idle          -> message it to start a fresh nightly run
#   - exists, busy (RUNNING) -> ask it to finish its log and restart itself
#
# The nightly routine itself lives in SKILL.md and always begins with /clear,
# so a fresh run never inherits the previous run's context.
set -euo pipefail

CARETAKER_NAME="caretaker"
CARETAKER_FILTER='labels.caretaker == "true"'

ROUTINE_MESSAGE="It is time for your nightly run. Follow your caretaker skill now: begin by running /clear, then carry out the routine documented in .agents/skills/caretaker/SKILL.md."

WRAPUP_MESSAGE="A new day's nightly run is due while you are still mid-run. Please finish writing your current run log now, then restart yourself for the new day by following your caretaker skill from the top (begin with /clear)."

log() { printf '%s run_caretaker: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Resolve the workspace label so the Caretaker tab groups with the user's other
# agents in the minds UI (mirrors libs/bootstrap's create-chat workspace logic:
# prefer the services agent's `workspace` label, fall back to the host_name).
resolve_workspace() {
  python3 - <<'PY'
import json, os, sys

host_dir = os.environ.get("MNGR_HOST_DIR", "")
agent_id = os.environ.get("MNGR_AGENT_ID", "")
if host_dir and agent_id:
    try:
        with open(os.path.join(host_dir, "agents", agent_id, "data.json")) as handle:
            workspace = json.load(handle).get("labels", {}).get("workspace")
        if workspace:
            print(workspace)
            sys.exit(0)
    except (OSError, ValueError):
        pass
if host_dir:
    try:
        with open(os.path.join(host_dir, "data.json")) as handle:
            print(json.load(handle).get("host_name", ""))
            sys.exit(0)
    except (OSError, ValueError):
        pass
print("")
PY
}

# Active Caretaker agent ids (one per line; empty if none).
caretaker_ids() {
  uv run mngr list --active --include "$CARETAKER_FILTER" --ids --on-error continue 2>/dev/null || true
}

# Ids of Caretaker agents currently RUNNING (mid-turn). `--running` is mngr's
# alias for `--include 'state == "RUNNING"'`, so we never parse state strings.
running_caretaker_ids() {
  uv run mngr list --active --running --include "$CARETAKER_FILTER" --ids --on-error continue 2>/dev/null || true
}

create_caretaker() {
  local workspace label_args=()
  workspace="$(resolve_workspace)"
  if [ -n "$workspace" ]; then
    label_args=(--label "workspace=${workspace}")
  fi
  log "no Caretaker found; creating one"
  uv run mngr create "$CARETAKER_NAME" \
    --transfer none \
    --template caretaker \
    --no-connect \
    --format json \
    --label caretaker=true \
    --label auto_created=true \
    "${label_args[@]}" \
    --message "$ROUTINE_MESSAGE"
}

main() {
  local ids first_id running
  ids="$(caretaker_ids)"

  if [ -z "${ids//[[:space:]]/}" ]; then
    create_caretaker
    log "Caretaker created"
    return 0
  fi

  first_id="$(printf '%s\n' "$ids" | head -n1)"
  running="$(running_caretaker_ids)"

  if printf '%s\n' "$running" | grep -qxF "$first_id"; then
    log "Caretaker ${first_id} is mid-run; sending wrap-up + self-restart message"
    uv run mngr message "$first_id" --message "$WRAPUP_MESSAGE"
  else
    log "Caretaker ${first_id} is idle; sending a fresh nightly run message"
    uv run mngr message "$first_id" --start --message "$ROUTINE_MESSAGE"
  fi
}

main "$@"
