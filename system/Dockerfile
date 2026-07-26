FROM python:3.12-slim-trixie

# /root/.local/bin holds uv + claude (installed by scripts/setup_system.sh); put
# it on PATH for every build layer and at runtime.
ENV PATH="/root/.local/bin:$PATH"

# Pin Claude Code; passed to setup_system.sh and recorded for the runtime version
# check. Keep in sync with agent_types.claude.version in .mngr/settings.toml and
# the default in scripts/setup_system.sh. Bump deliberately, not by accident.
ARG CLAUDE_CODE_VERSION=2.1.207
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# ============================================================================
# System toolchain (repo-independent). Shared verbatim with the Lima provider,
# which runs this exact script in the VM. setup_system.sh installs the system
# toolchain AND invokes install_secret_scanners.sh to bake the publish-
# inspiration scan gate's secret-scanner binaries (betterleaks + kingfisher),
# so both docker-built images and Lima VMs get the scanners from the same
# common script. Copied with its sibling _provision_guard.sh (which
# setup_system.sh sources via `dirname "$0"`) and install_secret_scanners.sh
# (which it invokes the same way) and nothing else, so this expensive, stable
# layer caches against these scripts + pinned versions, not application source.
# install_secret_scanners.sh is the single source of truth for the version
# pins and per-arch sha256s; it stays installed at
# /usr/local/bin/default-workspace-template-install-secret-scanners so it is runnable by hand
# if a scanner ever goes missing (the scan gate's error names that command).
# Caching tradeoff: folding the scanner install into this layer means a pin
# bump in install_secret_scanners.sh now rebuilds the whole system-toolchain
# layer (apt/node/uv + scanners) rather than a scanner-only layer -- accepted
# so a single common script covers docker AND Lima.
# ============================================================================
# Pin every apt operation (this build and all runtime installs) to the
# committed archive snapshot timestamp. Runs before any apt usage so the
# system-toolchain layer below resolves against the frozen universe; the
# script and timestamp ride their own COPY so a T bump invalidates exactly
# the layers that depend on package versions.
COPY scripts/write_apt_sources.sh /usr/local/bin/default-workspace-template-write-apt-sources
COPY .mngr/apt-snapshot-timestamp /etc/default-workspace-template-apt-snapshot-timestamp
RUN chmod +x /usr/local/bin/default-workspace-template-write-apt-sources \
    && default-workspace-template-write-apt-sources "$(cat /etc/default-workspace-template-apt-snapshot-timestamp)"

COPY scripts/setup_system.sh /usr/local/bin/default-workspace-template-setup-system
COPY scripts/_provision_guard.sh /usr/local/bin/_provision_guard.sh
COPY scripts/install_secret_scanners.sh /usr/local/bin/default-workspace-template-install-secret-scanners
RUN chmod +x /usr/local/bin/default-workspace-template-setup-system /usr/local/bin/default-workspace-template-install-secret-scanners && default-workspace-template-setup-system

# /var/cache/user is the off-volume cache target, pinned via ENV so BOTH the
# build layers below (dependency pre-warm) and the runtime container cache to
# the same image-layer path: warm caches stay baked into the image instead of
# landing under /home/user (where the runtime mount would shadow them), and
# runtime installs reuse them. The seed script additionally symlinks ~/.cache
# here so even non-XDG-aware tools land off-volume. Caches are never backed up.
ENV XDG_CACHE_HOME=/var/cache/user
ENV UV_CACHE_DIR=/var/cache/user/uv
ENV npm_config_cache=/var/cache/user/npm

# ============================================================================
# Pre-COPY manifest layer.
# Copies only the dependency manifests (no application source) so the
# expensive dependency install below caches against dependency-manifest
# changes only. Application code edits land on the `COPY . /home/user/workspace/`
# further down -- they do not invalidate the cache here.
# ============================================================================
WORKDIR /home/user/workspace/

# Root + per-workspace-member pyproject.toml + uv.lock.
COPY pyproject.toml uv.lock /home/user/workspace/
COPY libs/app_watcher/pyproject.toml /home/user/workspace/libs/app_watcher/pyproject.toml
COPY libs/bootstrap/pyproject.toml /home/user/workspace/libs/bootstrap/pyproject.toml
COPY libs/cloudflare_tunnel/pyproject.toml /home/user/workspace/libs/cloudflare_tunnel/pyproject.toml
COPY libs/github_sync/pyproject.toml /home/user/workspace/libs/github_sync/pyproject.toml
COPY apps/system_interface/pyproject.toml /home/user/workspace/apps/system_interface/pyproject.toml

# vendor/mngr path-dependency manifests. The root pyproject.toml's
# [tool.uv.sources] points imbue-common, imbue-mngr, imbue-mngr-claude,
# resource-guards, and concurrency-group at vendor/mngr/libs/<pkg>; uv
# needs each pyproject.toml present to resolve the workspace. mngr_modal
# and mngr_wait are also workspace members whose transitive deps benefit
# from pre-warming even though only mngr_wait is registered post-COPY
# (as a mngr plugin, not a tool install).
COPY vendor/mngr/libs/imbue_common/pyproject.toml /home/user/workspace/vendor/mngr/libs/imbue_common/pyproject.toml
COPY vendor/mngr/libs/mngr/pyproject.toml /home/user/workspace/vendor/mngr/libs/mngr/pyproject.toml
COPY vendor/mngr/libs/mngr_claude/pyproject.toml /home/user/workspace/vendor/mngr/libs/mngr_claude/pyproject.toml
COPY vendor/mngr/libs/mngr_modal/pyproject.toml /home/user/workspace/vendor/mngr/libs/mngr_modal/pyproject.toml
COPY vendor/mngr/libs/mngr_wait/pyproject.toml /home/user/workspace/vendor/mngr/libs/mngr_wait/pyproject.toml
COPY vendor/mngr/libs/resource_guards/pyproject.toml /home/user/workspace/vendor/mngr/libs/resource_guards/pyproject.toml
COPY vendor/mngr/libs/concurrency_group/pyproject.toml /home/user/workspace/vendor/mngr/libs/concurrency_group/pyproject.toml

# Frontend npm manifest (lockfile + package.json) -- install needs only these.
COPY apps/system_interface/frontend/package.json apps/system_interface/frontend/package-lock.json /home/user/workspace/apps/system_interface/frontend/

# Dependency install (manifests only). Shared verbatim with the Lima provider.
COPY scripts/install_dependencies.sh /usr/local/bin/default-workspace-template-install-dependencies
RUN chmod +x /usr/local/bin/default-workspace-template-install-dependencies && default-workspace-template-install-dependencies

# ============================================================================
# End pre-COPY manifest layer. Source-changing layers begin below.
# ============================================================================

# copy in all of our code:
COPY . /home/user/workspace/

# Build the workspace from full source. Shared verbatim with the Lima provider.
RUN bash /home/user/workspace/scripts/build_workspace.sh

# Move the baked workspace off the volume mount path so the shipped
# image has /home/user/workspace/ EMPTY. At runtime, /home/user is a persistent
# volume mount; any image-layer content sitting at /home/user/workspace/ would
# be shadowed by the mount. /docker_build_code holds the workspace
# until first boot, where the post-host-create seed step (see below)
# atomically relocates it onto the volume.
RUN mv /home/user/workspace /docker_build_code

# The user-data layout: the per-host persistent volume mounts at /home/user
# (the ONE persistent tree -- workspace checkout, worktrees, mngr state at
# ~/.mngr, dotfiles), and root's home moves there so $HOME, passwd, and
# sshd's authorized_keys lookup all agree at RUNTIME. This is deliberately
# the LAST build step: every earlier layer must keep HOME=/root so the
# build-time toolchain (uv, claude, uv-tool shims from setup_system and
# build_workspace) lands in /root/.local -- image content, reachable via the
# ENV PATH at the top -- and never under /home/user, where the runtime mount
# would shadow it. Direct passwd edit rather than `usermod -d`: usermod
# refuses to modify a user with running processes (exit 8), and root always
# has one in a build container (this very shell) or a live VM.
RUN sed -i 's|^root:\([^:]*:[^:]*:[^:]*:[^:]*\):/root:|root:\1:/home/user:|' /etc/passwd \
    && grep -q '^root:.*:/home/user:' /etc/passwd \
    && mkdir -p /home/user /var/cache/user

# Install the first-boot seed script at a stable image-layer path. It
# has to live OUTSIDE /home/user (the volume mount path) so the runtime
# mount does not shadow it, and OUTSIDE /docker_build_code (which the
# seed itself cleans up after relocating). /usr/local/bin/ is on PATH,
# is image-layer, and survives the bake-and-relocate dance.
#
# mngr invokes this script synchronously via the `post_host_create_command`
# create-template hook (see .mngr/settings.toml) after the host is online
# but before any agent work_dir setup -- the seed therefore has the
# volume mount, the home-skeleton dance, and sshd all in place, and
# completes before anything else writes to /home/user/workspace.
#
# The seed/relocate dance is docker-volume-specific; the Lima provider does
# not use it (the project syncs straight onto the VM's btrfs /home/user/.mngr disk).
#
# Source mode bits are already +x; chmod is defensive in case the file
# is checked out without exec bits.
COPY scripts/default_workspace_template_seed.sh /usr/local/bin/default-workspace-template-seed
RUN chmod +x /usr/local/bin/default-workspace-template-seed
