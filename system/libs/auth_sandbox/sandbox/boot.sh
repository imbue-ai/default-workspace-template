#!/bin/bash
# The auth sandbox's command (the web latchkey auth sandbox spec, section 5.3): start everything that
# depends on no user, then wait. The connector writes the share materials, the TLS material, the grants
# and the owner's public key after this is running; the share gateway picks share.env up on its next
# tick, and owner-exec verifies against ~/.ssh/authorized_keys on every request.
#
# Reads the non-secret environment the connector passes at create: AUTH_SANDBOX_HOST_ID (owner-exec's
# audience is container:<that id>), and AUTH_SANDBOX_REGION / AUTH_SANDBOX_WORKSPACE_HOST_ID, which the
# sign-in command reads later from the same environment. Installed at /opt/auth-sandbox/boot.sh by
# system/scripts/build_auth_sandbox_image.py; its source is system/libs/auth_sandbox/sandbox/boot.sh.
set -euo pipefail

readonly REPO_ROOT="/home/user/workspace"
readonly INSTALL_DIR="/opt/auth-sandbox"
readonly RUNTIME_DIR="/run/auth-sandbox"
readonly SCREEN_URL="http://127.0.0.1:8090"
# How long the Screen service gets to answer its health route before the boot gives up on starting
# the stack early (a viewer's first page load starts it too, so this is a head start, not a gate).
readonly SCREEN_READY_TIMEOUT_SECONDS=120

: "${AUTH_SANDBOX_HOST_ID:?AUTH_SANDBOX_HOST_ID must carry the sandbox host id}"

# The image moved root's home to /home/user; pin it so owner-exec's authorized_keys path, the XFCE
# session and the commands all agree whatever the sandbox runtime set.
export HOME="/home/user"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Everything user-specific lives under the tmpfs runtime dir, never on the snapshotted filesystem.
umask 077
mkdir -p "$RUNTIME_DIR/incoming" "$RUNTIME_DIR/export" "$RUNTIME_DIR/latchkey" "$RUNTIME_DIR/host"
openssl rand -base64 32 > "$RUNTIME_DIR/latchkey_key"
cp "$INSTALL_DIR/config.json" "$RUNTIME_DIR/latchkey/config.json"
# The host record owner-exec reads its audience from, in the shape $MNGR_HOST_DIR/data.json has on a
# workspace (system/scripts/run_owner_exec.sh).
python3 -c 'import json, os, sys; json.dump({"host_id": os.environ["AUTH_SANDBOX_HOST_ID"]}, sys.stdout)' \
    > "$RUNTIME_DIR/host/data.json"
mkdir -p "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"
umask 022

export MNGR_HOST_DIR="$RUNTIME_DIR/host"
export LATCHKEY_DIRECTORY="$RUNTIME_DIR/latchkey"
export LATCHKEY_DISABLE_COUNTING=1
# The sandbox has no gateway; a value inherited from the image would send local latchkey runs to it.
unset LATCHKEY_GATEWAY LATCHKEY_GATEWAY_PASSWORD LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE

mkdir -p /var/log/supervisor "$REPO_ROOT/data/.secrets" "$REPO_ROOT/data/.state"
cd "$REPO_ROOT"

supervisord -n -c "$INSTALL_DIR/supervisord.conf" &
SUPERVISORD_PID=$!
trap 'kill -TERM "$SUPERVISORD_PID" 2>/dev/null || true' TERM INT

# Start the Screen's stack (Xorg, XFCE, PulseAudio, neko) before any viewer arrives.
for ((waited = 0; waited < SCREEN_READY_TIMEOUT_SECONDS; waited++)); do
    if curl -fsS --max-time 2 "$SCREEN_URL/health" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$SUPERVISORD_PID" 2>/dev/null; then
        echo "auth-sandbox boot: supervisord exited before the Screen service answered" >&2
        exit 1
    fi
    sleep 1
done
if curl -fsS --max-time 60 -X POST "$SCREEN_URL/api/start" >/dev/null; then
    echo "auth-sandbox boot: the Screen's stack is starting"
else
    echo "auth-sandbox boot: the Screen's stack did not start early; a viewer's first load starts it" >&2
fi

wait "$SUPERVISORD_PID"
