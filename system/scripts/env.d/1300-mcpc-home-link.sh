#!/usr/bin/env bash
# env.d unit: point ~/.mcpc, the state directory of mcpc (the MCP client every
# agent, app, and scheduled job shares), at data/.secrets/mcpc.
#
# mcpc keeps its sessions, OAuth sign-ins, and stored headers in ~/.mcpc. Every
# process here has HOME=/home/user (a cron job's comes from root's passwd entry),
# so this one link puts all of them on the same store, under the secrets guard
# and the backup. It lives in env.d rather than only in the first-boot seed
# because env-converge also runs on every boot and during an update-self apply,
# which is how a workspace updated in place gets the link. The seed runs this
# same script, so a new workspace has the link before its first agent starts.
#
# env.d contract: idempotent with a fast satisfied-check -- no markers.
set -euo pipefail

STORE=/home/user/workspace/data/.secrets/mcpc
LINK=/home/user/.mcpc

if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$STORE" ] && [ -d "$STORE" ] \
    && [ "$(stat -c %a "$STORE")" = 700 ]; then
    echo "[env.d/mcpc-home-link] ~/.mcpc already links to the store, satisfied"
    exit 0
fi

mkdir -p "$STORE"

if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
    # mcpc ran before the link existed: keep what it stored, without overwriting
    # anything already in the store.
    echo "[env.d/mcpc-home-link] moving an existing ~/.mcpc into the store"
    cp -an "$LINK"/. "$STORE"/
    rm -rf "$LINK"
elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
    rm -f "$LINK"
fi

# After the copy, which carries the old directory's own mode onto the store.
chmod 700 "$STORE"
ln -s "$STORE" "$LINK"
echo "[env.d/mcpc-home-link] linked ~/.mcpc to the store"
