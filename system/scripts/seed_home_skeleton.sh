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

# Set up the pi coding extensions that bring the pi agent up to parity with
# the claude/codex agents: pi-subagents (delegate to subagents) and
# pi-web-access (fetch/search the web). This runs here -- at runtime, on the
# persistent volume -- rather than beside the pi CLI install in setup_system.sh,
# because ~/.pi lives under HOME and the runtime volume mount shadows the
# build-time HOME (/root).
#
# Fast path: the image bakes the extensions' installed npm tree at
# /opt/pi-extensions (setup_system.sh), so seeding is a local copy of that tree
# plus a jq merge of the package pins into settings.json -- no network, ~1s.
# `pi install` is only the fallback for images built before the bake; it re-runs
# a full npm install even when the tree already exists (~30-60s + network).
# Versions are pinned to match the image bake (setup_system.sh /
# PI_SUBAGENTS_VERSION / PI_WEB_ACCESS_VERSION); bump all together,
# deliberately. Best-effort throughout: a failure must not abort the rest of
# the seed, and the grep guard skips work when the entry is already present.
# The npm_config_* vars reach the npm that `pi install` shells out to, and keep
# the fallback off npm's audit round trip -- see setup_system.sh for why.
if command -v pi >/dev/null 2>&1; then
    if [ -d /opt/pi-extensions/npm/node_modules ] && [ ! -d /home/user/.pi/agent/npm ]; then
        mkdir -p /home/user/.pi/agent
        cp -a /opt/pi-extensions/npm /home/user/.pi/agent/npm \
            || echo "seed_home_skeleton: warning: failed to copy baked pi extensions" >&2
    fi
    for pi_ext in npm:pi-subagents@0.45.0 npm:pi-web-access@0.19.0; do
        if ! grep -q "\"${pi_ext}\"" /home/user/.pi/agent/settings.json 2>/dev/null; then
            if [ -d /home/user/.pi/agent/npm/node_modules ] && command -v jq >/dev/null 2>&1; then
                # Tree already present (baked copy above, or a prior install):
                # register the package pin without touching npm. Merge, never
                # clobber -- other settings keys are preserved.
                mkdir -p /home/user/.pi/agent
                [ -s /home/user/.pi/agent/settings.json ] || printf '{}\n' > /home/user/.pi/agent/settings.json
                if jq --arg ext "${pi_ext}" '.packages = ((.packages // []) + [$ext] | unique)' \
                    /home/user/.pi/agent/settings.json > /home/user/.pi/agent/settings.json.tmp; then
                    mv /home/user/.pi/agent/settings.json.tmp /home/user/.pi/agent/settings.json
                else
                    rm -f /home/user/.pi/agent/settings.json.tmp
                    echo "seed_home_skeleton: warning: failed to register pi extension ${pi_ext}" >&2
                fi
            else
                npm_config_audit=false npm_config_fund=false \
                    PI_CODING_AGENT_DIR=/home/user/.pi/agent pi install "${pi_ext}" \
                    || echo "seed_home_skeleton: warning: failed to register pi extension ${pi_ext}" >&2
            fi
        fi
    done
fi
