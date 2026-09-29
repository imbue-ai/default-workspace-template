#!/usr/bin/env bash
# env.d unit: point ~/.mcpc, the state directory of mcpc (the MCP client every
# agent, app, and scheduled job shares), at data/.secrets/mcpc.
#
# mcpc keeps its sessions, OAuth sign-ins, and stored headers in $HOME/.mcpc.
# HOME is /home/user, root's passwd home, for agents, apps, and cron jobs, except
# on a lima VM booted without the desktop app: its boot unit
# (minds_lima_autostart.sh) starts system-services with HOME=/root, which the
# tmux server it forks passes to every process started there. So both homes link
# to the one store, under the secrets guard and the backup. This lives in env.d
# rather than only in the first-boot seed because env-converge also runs on
# every boot and during an update-self apply, which is how a workspace updated
# in place gets the links. The seed runs this same script, so a new workspace
# has them before its first agent starts.
#
# env.d contract: idempotent with a fast satisfied-check -- no markers.
set -euo pipefail

STORE=/home/user/workspace/data/.secrets/mcpc
LINKS=(/home/user/.mcpc /root/.mcpc)

is_linked() {
    [ -L "$1" ] && [ "$(readlink "$1")" = "$STORE" ]
}

if [ -d "$STORE" ] && [ "$(stat -c %a "$STORE")" = 700 ] \
    && is_linked "${LINKS[0]}" && is_linked "${LINKS[1]}"; then
    echo "[env.d/mcpc-home-link] every ~/.mcpc already links to the store, satisfied"
    exit 0
fi

mkdir -p "$STORE"

for link in "${LINKS[@]}"; do
    is_linked "$link" && continue
    if [ -d "$link" ] && [ ! -L "$link" ]; then
        # mcpc ran before the link existed: keep what it stored, without
        # overwriting anything already in the store.
        echo "[env.d/mcpc-home-link] moving an existing $link into the store"
        cp -an "$link"/. "$STORE"/
        rm -rf "$link"
    elif [ -e "$link" ] || [ -L "$link" ]; then
        rm -f "$link"
    fi
    ln -s "$STORE" "$link"
    echo "[env.d/mcpc-home-link] linked $link to the store"
done

# After any copy, which carries the old directory's own mode onto the store.
chmod 700 "$STORE"
