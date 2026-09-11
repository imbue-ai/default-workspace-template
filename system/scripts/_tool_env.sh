#!/usr/bin/env bash
# Pin where this workspace's uv-managed tools are installed.
#
# uv's tool directories follow $HOME, and build_workspace.sh runs under two of them: the
# image build's HOME=/root, and HOME=/home/user on a live create (root's passwd home at
# runtime). Left unpinned a create installs a second copy of every tool under
# /home/user/.local, which no update refreshes and which a login shell finds first. See
# tool_env.py for what that costs and for the cleanup that removes such a copy; this file
# is only the part that cannot be Python, because exporting into the *calling* shell is
# something a child process cannot do.
#
# The tool directories are pinned rather than $HOME itself because build_workspace.sh also
# writes $HOME-relative state -- its `git config --global` safe.directory entry -- that a
# wholesale HOME pin would move. (On docker that entry already lands in /root/.gitconfig,
# which the runtime root never reads, so the pin would only regress the lima/modal creates;
# the narrow pin keeps this change out of that question entirely.) setup_system.sh has no
# such state and pins HOME instead.
#
# Usage (source it, then):
#   tool_env_pin    # export UV_TOOL_DIR/UV_TOOL_BIN_DIR, prepend PATH

# Strict mode. Callers already set this, so re-asserting is a no-op for them and keeps the
# library safe to source from anywhere.
set -euo pipefail

# The home every tool this workspace installs is reached through. Overridable for tests;
# tool_env.py reads the same variable, so both halves move together.
TOOL_ENV_HOME="${TOOL_ENV_HOME:-/root}"

tool_env_pin() {
    export UV_TOOL_DIR="$TOOL_ENV_HOME/.local/share/uv/tools"
    export UV_TOOL_BIN_DIR="$TOOL_ENV_HOME/.local/bin"
    export PATH="$TOOL_ENV_HOME/.local/bin:$PATH"
}
