#!/bin/sh
# Seed the persistent /home/user tree's skeleton. Shared by the docker
# first-boot seed (default_workspace_template_seed.sh) and the lima/modal
# provisioning commands (.mngr/settings.toml), so every provider produces the
# same home layout. Idempotent, and never overwrites existing user data.

set -e

# Worktree agents land here (worktree_base_folder in .mngr/settings.toml).
mkdir -p /home/user/worktrees

# ~/.cache deliberately points at the container-local /var/cache/user so even
# tools that hardcode ~/.cache (rather than honoring XDG_CACHE_HOME) keep
# their caches off the volume and out of backups.
mkdir -p /var/cache/user
if [ ! -e /home/user/.cache ] && [ ! -L /home/user/.cache ]; then
    ln -s /var/cache/user /home/user/.cache
fi

# Root's interactive shells read $HOME/.bashrc, which lives on the volume now;
# seed the PATH + mngr-env lines the image used to keep in /root/.bashrc.
if [ ! -e /home/user/.bashrc ]; then
    printf '%s\n' \
        'PATH="/root/.local/bin:$PATH"' \
        'if [ -f /home/user/.mngr/env ]; then set -a; . /home/user/.mngr/env; set +a; fi' \
        > /home/user/.bashrc
fi

# Outgoing ssh reads $HOME/.ssh/known_hosts, which lives on the volume now;
# seed it from the image's /root/.ssh copy (github.com host keys, written by
# setup_system.sh) so git-over-ssh does not block on interactive confirmation.
if [ ! -e /home/user/.ssh/known_hosts ] && [ -f /root/.ssh/known_hosts ]; then
    mkdir -p /home/user/.ssh
    chmod 700 /home/user/.ssh
    cp /root/.ssh/known_hosts /home/user/.ssh/known_hosts
    chmod 600 /home/user/.ssh/known_hosts
fi

# Stamp the data-layout version so every backup of /home/user self-describes
# which layout its paths follow.
mkdir -p /home/user/.mngr
if [ ! -e /home/user/.mngr/layout-version ]; then
    printf '2\n' > /home/user/.mngr/layout-version
fi

# Register the pi coding extensions that bring the pi agent up to parity with
# the claude/codex agents: pi-subagents (delegate to subagents) and
# pi-web-access (fetch/search the web). `pi install` appends each to the
# `packages` list in ~/.pi/agent/settings.json, merging rather than clobbering
# any other settings; mngr's pi_coding plugin then syncs that list into every
# per-agent config dir and pi auto-installs the packages on first launch. This
# runs here -- at runtime, on the persistent volume -- rather than beside the pi
# CLI install in setup_system.sh, because ~/.pi lives under HOME and the runtime
# volume mount shadows the build-time HOME (/root). Versions are pinned to match
# the pinned CLI toolchain; bump deliberately. PI_CODING_AGENT_DIR pins the
# config dir so this does not depend on HOME being set. Best-effort: a
# registration failure (e.g. transient npm) must not abort the rest of the seed,
# and the grep guard skips the network fetch when the entry is already present.
if command -v pi >/dev/null 2>&1; then
    for pi_ext in npm:pi-subagents@0.45.0 npm:pi-web-access@0.19.0; do
        if ! grep -q "\"${pi_ext}\"" /home/user/.pi/agent/settings.json 2>/dev/null; then
            PI_CODING_AGENT_DIR=/home/user/.pi/agent pi install "${pi_ext}" \
                || echo "seed_home_skeleton: warning: failed to register pi extension ${pi_ext}" >&2
        fi
    done
fi
