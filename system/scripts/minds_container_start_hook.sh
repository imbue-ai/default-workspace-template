#!/bin/bash
# The container's start hook: mngr's self-healing entrypoint runs this, detached,
# on every container start after sshd is up (the Dockerfile installs it at mngr's
# `CONTAINER_START_HOOK_PATH`).
#
# Docker's restart policy brings a workspace container back after an OOM kill, a
# docker daemon restart or a VM reboot with nothing in it but sshd: the services
# agent's tmux session, and with it supervisord and every service (the share
# gateway included), only come back through `mngr start`. The VM-side boot unit
# (`minds-outer-autostart.sh`) covers a VM reboot; this covers a restart of the
# container alone, and needs no desktop app.
#
# On the container's very first start the services agent does not exist yet
# (`mngr create` is about to make it), so there is nothing to relaunch and the
# hook exits. Otherwise it records the start in the agent's events (the user's
# record of what happened to their workspace; the hook cannot tell docker's own
# restart from a `mngr start` of a stopped workspace, which re-runs the
# entrypoint too) and hands over to the shared start script, which sources the
# host and agent env and runs `mngr start` (idempotent and flock-serialized, so
# racing the desktop client or the VM boot unit is safe).
set -euo pipefail

HOST_DIR=/home/user/.mngr
WORKSPACE=/home/user/workspace
START_SCRIPT="$WORKSPACE/system/scripts/minds_start_services_agent.sh"
RECORD_SCRIPT="$WORKSPACE/system/scripts/workspace_bare_scripts/record_container_restart.py"

services_agent_dir=""
for data_file in "$HOST_DIR"/agents/*/data.json; do
    [ -e "$data_file" ] || continue
    if [ "$(jq -r '.name // empty' "$data_file" 2>/dev/null)" = "system-services" ]; then
        services_agent_dir="$(dirname "$data_file")"
    fi
done
if [ -z "$services_agent_dir" ]; then
    echo "mngr-container-start-hook: no system-services agent on this host yet; nothing to relaunch" >&2
    exit 0
fi
if [ ! -x "$START_SCRIPT" ]; then
    echo "mngr-container-start-hook: $START_SCRIPT is missing; not relaunching the services agent" >&2
    exit 0
fi

python3 "$RECORD_SCRIPT" --agent-dir "$services_agent_dir" \
    || echo "mngr-container-start-hook: could not record the restart in $services_agent_dir" >&2

# A login shell, so uv and mngr are on PATH, as the VM boot unit runs it.
exec bash -lc "exec '$START_SCRIPT'"
