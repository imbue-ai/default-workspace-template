#!/usr/bin/env bash
# Where this workspace's uv-managed tools live, and removal of copies that shadow them.
#
# uv's tool directories follow $HOME, and the scripts that install tools run under two
# different ones: the image build's HOME=/root, and HOME=/home/user on a live create
# (root's passwd home at runtime). Left unpinned, a create installs a second copy of
# every tool under /home/user/.local. No update refreshes that copy, and a login shell
# finds it first. The desktop app's `mngr exec` and the terminal app both arrive through
# such a shell, so the stale copy is the one they run while the refreshed one reports
# success.
#
# The tool directories are pinned rather than $HOME itself because build_workspace.sh
# also writes $HOME-relative state -- its `git config --global` safe.directory entry --
# that a wholesale HOME pin would move. (On docker that entry already lands in
# /root/.gitconfig, which the runtime root never reads, so the pin would only regress
# the lima/modal creates; the narrow pin keeps this change out of that question
# entirely.) setup_system.sh has no such state and pins HOME instead.
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
# console script pointing into it.
#
# Scope: this runs only where build_workspace.sh does -- the image build and a create's
# provisioning -- so it keeps NEW workspaces out of the state. It does not reach one
# already in it: an existing workspace is not re-provisioned, and the update apply runs
# setup_system.sh, not this script. Those are cleaned by the apply's own
# update_environment.remove_shadowing_mngr_installs, and only once their update can
# start at all.
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
    # Decide the console script's fate BEFORE removing what it points into: only the one
    # resolving to the environment being removed goes with it, and that is judged by
    # device+inode, so a $HOME spelled differently now than when uv baked the shebang
    # (a trailing slash, a symlink) does not leave a dangling shim behind -- which would
    # be worse on PATH than the stale-but-working copy it replaced.
    _shadow_script="$_shadow_home/.local/bin/mngr"
    _is_shadow_script=false
    _shadow_shebang="$(head -n 1 "$_shadow_script" 2>/dev/null || true)"
    case "$_shadow_shebang" in
        "#!"*)
            # uv writes `#!<tool env>/bin/python`, so the environment is two levels up.
            _shadow_interpreter="${_shadow_shebang#\#!}"
            _shadow_interpreter="${_shadow_interpreter%% *}"
            if [ "$(dirname "$(dirname "$_shadow_interpreter")")" -ef "$_shadow_env" ]; then
                _is_shadow_script=true
            fi
            ;;
    esac
    echo "[tool-env] removing the mngr tool install under $_shadow_home/.local; it shadows" \
        "$TOOL_ENV_HOME/.local on every login shell's PATH and no update refreshes it."
    rm -rf "$_shadow_env"
    if [ "$_is_shadow_script" = true ]; then
        rm -f "$_shadow_script"
    fi
}
