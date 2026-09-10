#!/usr/bin/env bash
# Where this workspace's uv-managed tools live, and removal of copies that shadow them.
#
# uv's tool directories follow $HOME, and the scripts that install tools run under two
# different ones: the image build's HOME=/root, and HOME=/home/user on a live create
# (root's passwd home at runtime). Left unpinned, a create installs a second copy of
# every tool under /home/user/.local. No update refreshes that copy, and a login shell
# finds it first, because /home/user/.bashrc sources $HOME/.local/bin/env. The desktop
# app's `mngr exec` and the terminal app both arrive through such a shell, so the stale
# copy is the one they run while the refreshed one reports success.
#
# The tool directories are pinned rather than $HOME itself, deliberately:
# build_workspace.sh also writes $HOME-relative state that has to land in the *runtime*
# home (its `git config --global` safe.directory entry), which a wholesale HOME pin
# would move. setup_system.sh has no such state and pins HOME instead.
#
# Usage (source it, then):
#   tool_env_pin                    # export UV_TOOL_DIR/UV_TOOL_BIN_DIR, prepend PATH
#   tool_env_drop_shadowing_mngr    # delete an mngr tool env under a different $HOME

# Strict mode. Callers already set this, so re-asserting is a no-op for them and keeps
# the library safe to source from anywhere.
set -euo pipefail

# The home every tool this workspace installs is reached through. Overridable for tests.
TOOL_ENV_HOME="${TOOL_ENV_HOME:-/root}"

# The uv tool name of the vendored mngr distribution (its `[project] name`), which is
# what uv names its environment directory -- not the `mngr` console script.
_TOOL_ENV_MNGR_TOOL="imbue-mngr"

tool_env_pin() {
    export UV_TOOL_DIR="$TOOL_ENV_HOME/.local/share/uv/tools"
    export UV_TOOL_BIN_DIR="$TOOL_ENV_HOME/.local/bin"
    export PATH="$TOOL_ENV_HOME/.local/bin:$PATH"
}

# Remove an mngr tool environment installed under some other $HOME, along with the
# console script pointing into it. The update-self apply does the same for workspaces
# that can still run it (update_environment.remove_shadowing_mngr_installs); this covers
# the ones whose update cannot start precisely because the shadowing copy is what the
# app reaches.
#
# Call it only AFTER the pinned install has been made: it refuses to remove anything
# unless it can see the environment it is protecting, so a failed install never leaves
# the workspace with no mngr at all.
tool_env_drop_shadowing_mngr() {
    _shadow_home="${HOME:-}"
    [ -n "$_shadow_home" ] || return 0
    _shadow_env="$_shadow_home/.local/share/uv/tools/$_TOOL_ENV_MNGR_TOOL"
    _pinned_env="$TOOL_ENV_HOME/.local/share/uv/tools/$_TOOL_ENV_MNGR_TOOL"
    [ -d "$_shadow_env" ] || return 0
    [ -d "$_pinned_env" ] || return 0
    # Compared by device+inode, not by string: an image build reaches the pinned
    # environment through $HOME as well, and "/root/" or a symlinked home would make a
    # string comparison call the pinned install a shadow of itself and delete it.
    if [ "$_shadow_env" -ef "$_pinned_env" ]; then
        return 0
    fi
    echo "[tool-env] removing the mngr tool install under $_shadow_home/.local; it shadows" \
        "$TOOL_ENV_HOME/.local on every login shell's PATH and no update refreshes it."
    rm -rf "$_shadow_env"
    # Only the console script that points into what was just removed; one resolving to
    # the pinned environment is a working shim and stays.
    _shadow_script="$_shadow_home/.local/bin/mngr"
    _shadow_shebang="$(head -n 1 "$_shadow_script" 2>/dev/null || true)"
    case "$_shadow_shebang" in
        "#!$_shadow_env/"*) rm -f "$_shadow_script" ;;
    esac
}
