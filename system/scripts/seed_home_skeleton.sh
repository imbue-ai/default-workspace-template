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
