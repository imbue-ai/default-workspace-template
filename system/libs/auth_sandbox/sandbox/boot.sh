#!/bin/bash
# The auth sandbox's command (the web latchkey auth sandbox spec, section 5.3, and the warm pool's contract): lay
# out a blank sandbox from the environment the connector passes at create, start the claim watcher and supervisord,
# start the Screen's stack, and end the sandbox when the claimed session expires.
#
# The connector passes nothing about a user at create (a pooled sandbox has none yet): the sandbox's host id
# (owner-exec's audience, container:<that id>), its region (read later by the sign-in) and the hosted chrome's
# origin. owner-exec starts with an empty authorized_keys and the Screen gated with no owner token digest, so both
# refuse everyone but loopback, which is what the connector's readiness checks use. When the connector claims the
# sandbox for a user it writes /run/auth-sandbox-claim.json, which auth-sandbox-await-claim installs as
# authorized_keys and the Screen's digest file; the watcher returns at the session's expiry, and this script then
# stops supervisord and exits, which ends the Modal sandbox.
#
# Installed at /opt/auth-sandbox/boot.sh by auth_sandbox.modal_image_build; its source is
# system/libs/auth_sandbox/sandbox/boot.sh.
set -euo pipefail

readonly REPO_ROOT="/home/user/workspace"
readonly INSTALL_DIR="/opt/auth-sandbox"
readonly RUNTIME_DIR="/run/auth-sandbox"
readonly SCREEN_URL="http://127.0.0.1:8090"
readonly CLAIM_WATCHER="$INSTALL_DIR/venv/bin/auth-sandbox-await-claim"
# The connector writes this after it reads the sandbox's tunnels, before or after this script runs, so
# the script leaves it alone; the Screen reads it on every connect-info request.
readonly CONNECT_INFO_PATH="/run/auth-sandbox-connect-info.json"
# How long the Screen service gets to answer its health route before the boot gives up on starting
# the stack early (a viewer's first page load starts it too, so this is a head start, not a gate).
readonly SCREEN_READY_TIMEOUT_SECONDS=120

: "${AUTH_SANDBOX_HOST_ID:?AUTH_SANDBOX_HOST_ID must carry the sandbox host id}"
: "${AUTH_SANDBOX_CHROME_ORIGIN:?AUTH_SANDBOX_CHROME_ORIGIN must carry the hosted chrome origin}"
# Each lands in a file or a header, so a stray line break or separator would add a share.env line, an
# owner-exec config key or a policy directive the connector never sent.
if [[ ! "$AUTH_SANDBOX_CHROME_ORIGIN" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
    echo "auth-sandbox boot: AUTH_SANDBOX_CHROME_ORIGIN must be a bare origin, with no path or trailing slash" >&2
    exit 1
fi
if [[ ! "$AUTH_SANDBOX_HOST_ID" =~ ^host-[0-9a-f]{32}$ ]]; then
    echo "auth-sandbox boot: AUTH_SANDBOX_HOST_ID must be host- and 32 lower-case hex digits" >&2
    exit 1
fi

# The image moved root's home to /home/user; pin it so owner-exec's authorized_keys path, the XFCE
# session and the commands all agree whatever the sandbox runtime set.
export HOME="/home/user"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Everything user-specific lives under the tmpfs runtime dir, never on the snapshotted filesystem.
umask 077
mkdir -p "$RUNTIME_DIR/latchkey" "$RUNTIME_DIR/host"
cp "$INSTALL_DIR/config.json" "$RUNTIME_DIR/latchkey/config.json"
# The host record owner-exec reads its audience from, in the shape $MNGR_HOST_DIR/data.json has on a
# workspace (system/scripts/run_owner_exec.sh).
python3 -c 'import json, os, sys; json.dump({"host_id": os.environ["AUTH_SANDBOX_HOST_ID"]}, sys.stdout)' \
    > "$RUNTIME_DIR/host/data.json"
mkdir -p "$HOME/.ssh"
: > "$HOME/.ssh/authorized_keys"
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
export SCREEN_OWNER_TOKEN_SHA256_FILE="$RUNTIME_DIR/screen_token_sha256"
export SCREEN_FRAME_ANCESTORS="$AUTH_SANDBOX_CHROME_ORIGIN"
export SCREEN_CONNECT_INFO_PATH="$CONNECT_INFO_PATH"
# VP8, which every browser decodes over WebRTC (Firefox decodes H.264 only with Cisco's OpenH264
# plugin), whatever browser the user signs in from.
export SCREEN_VIDEO_CODEC="vp8"
# One display size, whatever the viewer's frame: latchkey's login window is Playwright's 1280x720
# viewport, a window of about 1288x805, and would overflow a display a small viewer shrank. The viewer
# scales the picture to its frame instead.
export SCREEN_FIXED_SIZE="1360x864"
# The control profile (30 fps, 4000 kbit/s) from the first frame: the sandbox exists for one user actively
# signing in, so the picture never drops to the idle or watch profile while nobody holds control or types.
export SCREEN_VIDEO_PROFILE="control"
# A desktop session without the XFCE panel: the user signs in through one browser window, and a panel
# would offer only what the sandbox does not have.
export SCREEN_DESKTOP_PANEL="off"
# neko as an ICE lite agent: the one candidate it is reached on is the Modal TCP tunnel, so it answers
# the viewer's checks and gathers nothing of its own.
export SCREEN_ICE_LITE="on"

mkdir -p /var/log/supervisor "$REPO_ROOT/data/.state"
cd "$REPO_ROOT"

# Started ahead of everything else: the connector claims a sandbox made on demand as soon as it creates it,
# and the chrome signs its first request once owner-exec and the Screen answer.
"$CLAIM_WATCHER" &
CLAIM_WATCHER_PID=$!
supervisord -n -c "$INSTALL_DIR/supervisord.conf" &
SUPERVISORD_PID=$!
IS_SIGNALLED=0
trap 'IS_SIGNALLED=1; kill -TERM "$CLAIM_WATCHER_PID" "$SUPERVISORD_PID" 2>/dev/null || true' TERM INT

# Start the Screen's stack (Xorg, XFCE, PulseAudio, neko) before any viewer arrives. The bound is on
# the clock, not on the number of probes: each one may wait on its own timeout.
SECONDS=0
while ((SECONDS < SCREEN_READY_TIMEOUT_SECONDS)); do
    if curl -fsS --max-time 2 "$SCREEN_URL/health" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$SUPERVISORD_PID" 2>/dev/null; then
        echo "auth-sandbox boot: supervisord exited before the Screen service answered" >&2
        kill -TERM "$CLAIM_WATCHER_PID" 2>/dev/null || true
        exit 1
    fi
    sleep 1
done
if curl -fsS --max-time 60 -X POST "$SCREEN_URL/api/start" >/dev/null; then
    echo "auth-sandbox boot: the Screen's stack is starting"
else
    echo "auth-sandbox boot: the Screen's stack did not start early; a viewer's first load starts it" >&2
fi

# The watcher returns at the session's expiry (or fails); supervisord returns only if it dies. Either one
# ending ends the sandbox, as does a signal (the trap above stops both, which ends this wait).
EXIT_STATUS=0
wait -n "$CLAIM_WATCHER_PID" "$SUPERVISORD_PID" || EXIT_STATUS=$?
if ((IS_SIGNALLED)); then
    echo "auth-sandbox boot: stopped by a signal; ending the sandbox" >&2
elif ! kill -0 "$SUPERVISORD_PID" 2>/dev/null; then
    echo "auth-sandbox boot: supervisord exited; ending the sandbox" >&2
    EXIT_STATUS=1
elif ((EXIT_STATUS == 0)); then
    echo "auth-sandbox boot: the session expired; ending the sandbox"
else
    echo "auth-sandbox boot: the claim watcher exited $EXIT_STATUS; ending the sandbox" >&2
fi
kill -TERM "$CLAIM_WATCHER_PID" "$SUPERVISORD_PID" 2>/dev/null || true
# A trapped signal interrupts a wait while supervisord is still stopping its children; the second wait
# lets it drain, so this script exits with supervisord rather than ahead of it.
wait "$SUPERVISORD_PID" 2>/dev/null || true
wait "$SUPERVISORD_PID" 2>/dev/null || true
exit "$EXIT_STATUS"
