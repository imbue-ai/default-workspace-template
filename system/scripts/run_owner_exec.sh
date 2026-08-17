#!/bin/bash
# supervisord launcher for the in-container ("inner") owner-exec service.
#
# Writes the daemon's TOML config from the workspace's runtime paths, then execs
# the pinned Go binary (installed at /usr/local/bin/owner-exec). The audience is
# the workspace share domain read from share.env (so it matches the hosted
# chrome's inner-audience binding and disables exec while unshared); the grants
# endpoints are enabled; responses are signed with the container's SSH host key.
set -euo pipefail

REPO_ROOT="${MINDS_WORKSPACE_ROOT:-/home/user/workspace}"
CONFIG_DIR="${REPO_ROOT}/data/.state"
CONFIG_PATH="${CONFIG_DIR}/owner_exec_inner.toml"
mkdir -p "$CONFIG_DIR"

# Resolve this workspace's mngr host id (host-<hex>) from the host record, so
# the daemon's fixed audience is container:<host-id>. Empty if unavailable, in
# which case the daemon falls back to the share-domain audience alone.
HOST_ID=""
if [ -n "${MNGR_HOST_DIR:-}" ] && [ -f "${MNGR_HOST_DIR}/data.json" ]; then
    HOST_ID="$(jq -r '.host_id // ""' "${MNGR_HOST_DIR}/data.json" 2>/dev/null || true)"
fi

cat > "$CONFIG_PATH" <<TOML
role = "inner"
host_id = "${HOST_ID}"
listen_host = "127.0.0.1"
listen_port = 8793
repo_root = "${REPO_ROOT}"
authorized_keys_path = "${HOME}/.ssh/authorized_keys"
host_key_path = "/etc/ssh/ssh_host_ed25519_key"
grants_enabled = true
share_env_path = "${REPO_ROOT}/data/.secrets/share.env"
register_port = true
service_name = "owner-exec"
forward_port_script = "${REPO_ROOT}/system/scripts/forward_port.py"
TOML

exec /usr/local/bin/owner-exec --config "$CONFIG_PATH"
