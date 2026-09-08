#!/bin/bash
# One-shot: register the VM-resident owner-exec ("vm-exec") service in apps.toml
# so the local forward and the share gateway route to it, exactly like any other
# workspace service. The vm instance listens on the container's default gateway
# (the VM side of the docker bridge); it exists only on imbue-cloud slices / VPS
# outers, so on a local docker/lima workspace the probe fails and nothing is
# registered (the service simply does not appear in /_health).
#
# Idempotent: re-registering an already-present service is a no-op upsert.
set -euo pipefail

REPO_ROOT="${MINDS_WORKSPACE_ROOT:-/home/user/workspace}"
VM_EXEC_PORT="${OWNER_EXEC_VM_PORT:-8794}"

# The VM is the container's default gateway. Resolve it rather than hardcoding
# 172.17.0.1 so a custom docker network still works.
#
# Both the `command -v` probe and the `|| true` are load-bearing under `set -euo pipefail`:
# iproute2 is absent on some hosts, and a bare failing pipeline exits 127 BEFORE the skip
# branch below, so supervisord logs "exit status 127; not expected" on every boot of a host
# this script is meant to skip cleanly.
if ! command -v ip >/dev/null 2>&1; then
    echo "register_vm_exec: no 'ip' command; skipping vm-exec registration" >&2
    exit 0
fi
gateway="$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}' || true)"
if [ -z "${gateway:-}" ]; then
    echo "register_vm_exec: no default gateway; skipping vm-exec registration" >&2
    exit 0
fi

alive_url="http://${gateway}:${VM_EXEC_PORT}/_alive"
if ! curl -fsS --max-time 5 "$alive_url" >/dev/null 2>&1; then
    echo "register_vm_exec: no vm-exec at ${alive_url} (not a slice/VPS outer); skipping" >&2
    exit 0
fi

# --internal: vm-exec is an exec API with no page of its own, so it must not
# be offered as an openable app in the launcher or the All apps popover.
python3 "${REPO_ROOT}/system/scripts/forward_port.py" \
    --name vm-exec \
    --url "http://${gateway}:${VM_EXEC_PORT}" \
    --internal
