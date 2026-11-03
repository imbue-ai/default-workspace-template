#!/bin/bash
# The auth sandbox's command (the web latchkey auth sandbox spec, section 5.3): lay out the sandbox from
# the environment the connector passes at create, start supervisord, start the Screen's stack, and wait.
# The connector passes no secrets: the owner's public key, the hosted chrome's origin and the SHA-256 of
# the Screen's owner token. The Screen and owner-exec listen on every interface, since Modal's
# encrypted tunnels reach them from outside the sandbox, and the Screen gates every request that is not
# loopback (system/apps/screen/README.md, "Gate mode").
#
# AUTH_SANDBOX_HOST_ID is owner-exec's audience (container:<that id>); AUTH_SANDBOX_REGION and
# AUTH_SANDBOX_WORKSPACE_HOST_ID are read later by the sign-in command from the same environment.
# Installed at /opt/auth-sandbox/boot.sh by auth_sandbox.modal_image_build; its source is
# system/libs/auth_sandbox/sandbox/boot.sh.
set -euo pipefail

readonly REPO_ROOT="/home/user/workspace"
readonly INSTALL_DIR="/opt/auth-sandbox"
readonly RUNTIME_DIR="/run/auth-sandbox"
readonly SCREEN_URL="http://127.0.0.1:8090"
# The connector writes this after it reads the sandbox's tunnels, before or after this script runs, so
# the script leaves it alone; the Screen reads it on every connect-info request.
readonly CONNECT_INFO_PATH="/run/auth-sandbox-connect-info.json"
# How long the Screen service gets to answer its health route before the boot gives up on starting
# the stack early (a viewer's first page load starts it too, so this is a head start, not a gate).
readonly SCREEN_READY_TIMEOUT_SECONDS=120

: "${AUTH_SANDBOX_HOST_ID:?AUTH_SANDBOX_HOST_ID must carry the sandbox host id}"
: "${AUTH_SANDBOX_AUTHORIZED_KEY:?AUTH_SANDBOX_AUTHORIZED_KEY must carry the owner OpenSSH public key line}"
: "${AUTH_SANDBOX_CHROME_ORIGIN:?AUTH_SANDBOX_CHROME_ORIGIN must carry the hosted chrome origin}"
: "${AUTH_SANDBOX_SCREEN_TOKEN_SHA256:?AUTH_SANDBOX_SCREEN_TOKEN_SHA256 must carry the SHA-256 of the Screen owner token}"
# Each lands in a file or a header, so a stray line break or separator would add a key, a share.env
# line, an owner-exec config key or a policy directive the connector never sent.
if [[ "$AUTH_SANDBOX_AUTHORIZED_KEY" == *$'\n'* || "$AUTH_SANDBOX_AUTHORIZED_KEY" == *$'\r'* ]]; then
    echo "auth-sandbox boot: AUTH_SANDBOX_AUTHORIZED_KEY must be one line" >&2
    exit 1
fi
if [[ ! "$AUTH_SANDBOX_CHROME_ORIGIN" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
    echo "auth-sandbox boot: AUTH_SANDBOX_CHROME_ORIGIN must be a bare origin, with no path or trailing slash" >&2
    exit 1
fi
if [[ ! "$AUTH_SANDBOX_HOST_ID" =~ ^host-[0-9a-f]{32}$ ]]; then
    echo "auth-sandbox boot: AUTH_SANDBOX_HOST_ID must be host- and 32 lower-case hex digits" >&2
    exit 1
fi
if [[ ! "$AUTH_SANDBOX_SCREEN_TOKEN_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "auth-sandbox boot: AUTH_SANDBOX_SCREEN_TOKEN_SHA256 must be 64 lower-case hex digits" >&2
    exit 1
fi

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
printf '%s\n' "$AUTH_SANDBOX_AUTHORIZED_KEY" > "$HOME/.ssh/authorized_keys"
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"
# owner-exec reads its CORS origin from share.env, and only from `export KEY=value` lines.
mkdir -p "$REPO_ROOT/data/.secrets"
printf 'export SHARE_CHROME_ORIGIN=%s\n' "$AUTH_SANDBOX_CHROME_ORIGIN" > "$REPO_ROOT/data/.secrets/share.env"
umask 022

export MNGR_HOST_DIR="$RUNTIME_DIR/host"
export LATCHKEY_DIRECTORY="$RUNTIME_DIR/latchkey"
export LATCHKEY_DISABLE_COUNTING=1
# The sandbox has no gateway; a value inherited from the image would send local latchkey runs to it.
unset LATCHKEY_GATEWAY LATCHKEY_GATEWAY_PASSWORD LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE

# What the Screen and owner-exec read at start (supervisord's programs inherit its environment).
export SCREEN_LISTEN_HOST="0.0.0.0"
export OWNER_EXEC_LISTEN_HOST="0.0.0.0"
export SCREEN_OWNER_TOKEN_SHA256="$AUTH_SANDBOX_SCREEN_TOKEN_SHA256"
export SCREEN_FRAME_ANCESTORS="$AUTH_SANDBOX_CHROME_ORIGIN"
export SCREEN_CONNECT_INFO_PATH="$CONNECT_INFO_PATH"

mkdir -p /var/log/supervisor "$REPO_ROOT/data/.state"
cd "$REPO_ROOT"

supervisord -n -c "$INSTALL_DIR/supervisord.conf" &
SUPERVISORD_PID=$!
trap 'kill -TERM "$SUPERVISORD_PID" 2>/dev/null || true' TERM INT

# Start the Screen's stack (Xorg, XFCE, PulseAudio, neko) before any viewer arrives. The bound is on
# the clock, not on the number of probes: each one may wait on its own timeout.
SECONDS=0
while ((SECONDS < SCREEN_READY_TIMEOUT_SECONDS)); do
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

# A trapped signal interrupts the first wait while supervisord is still stopping its children; the
# second one lets it drain, so this script exits with supervisord rather than ahead of it.
wait "$SUPERVISORD_PID" || true
wait "$SUPERVISORD_PID"
