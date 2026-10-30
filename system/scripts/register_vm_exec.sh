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
# iproute2 is absent from the product image, so the kernel's route table is the
# fallback; its gateway column is the address in little-endian hex. Every
# probe ends in `|| true`: under `set -euo pipefail` a failing pipeline would
# exit before the skip branch below, which supervisord logs as an unexpected exit.
ROUTE_TABLE="${REGISTER_VM_EXEC_ROUTE_TABLE:-/proc/net/route}"
default_gateway_from_route_table() {
    local iface destination hex_gateway rest
    while read -r iface destination hex_gateway rest; do
        if [ "$destination" = "00000000" ] && [ "$hex_gateway" != "00000000" ]; then
            printf '%d.%d.%d.%d\n' "$((16#${hex_gateway:6:2}))" "$((16#${hex_gateway:4:2}))" \
                "$((16#${hex_gateway:2:2}))" "$((16#${hex_gateway:0:2}))"
            return 0
        fi
    done < <(tail -n +2 "$ROUTE_TABLE" 2>/dev/null || true)
}
if command -v ip >/dev/null 2>&1; then
    gateway="$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}' || true)"
else
    gateway="$(default_gateway_from_route_table || true)"
fi
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
