#!/usr/bin/env bash
# Shared workspace build for forever-claude-template hosts.
#
# Builds the workspace from full source: builds the frontend, installs the mngr /
# system-interface tools and their plugins, registers the editable workspace +
# vendored mngr packages, and exposes the tk ticket tracker. Needs the full repo
# present, so the Dockerfile runs it after copying all source and the Lima
# provider runs it after the repo is synced into the VM. Runs as root and is
# idempotent.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH="/root/.local/bin:$PATH"

REPO_ROOT="${REPO_ROOT:-/mngr/code}"
cd "$REPO_ROOT"

# Mark the repo a git safe.directory so in-container/in-VM git commands don't
# refuse on an ownership mismatch.
git config --global --add safe.directory "$REPO_ROOT"

# Build the system_interface frontend (deps installed by install_dependencies.sh).
( cd "$REPO_ROOT/apps/system_interface/frontend" && npm run build )

# Install mngr and system-interface as tools (both need the plugin packages so
# they can parse plugin-specific config). mngr_modal is intentionally not
# registered (providers.modal.is_enabled=false).
uv tool install -e "$REPO_ROOT/vendor/mngr/libs/mngr"
uv tool install -e "$REPO_ROOT/apps/system_interface" \
    --with-editable "$REPO_ROOT/vendor/mngr/libs/mngr_claude"
mngr plugin add \
    --path vendor/mngr/libs/mngr_claude \
    --path vendor/mngr/libs/mngr_wait

# Sync the workspace venv (registers the editable workspace + path deps). --frozen
# asserts the lockfile is canonical so the pre-warmed cache is not bypassed.
uv sync --all-packages --frozen

# Expose the vendored tk ticket tracker on PATH. The target resolves once
# /mngr/code is in place (on docker, after the first-boot seed).
ln -sf "$REPO_ROOT/vendor/tk/ticket" /usr/local/bin/tk
ln -sf "$REPO_ROOT/vendor/tk/ticket" /usr/local/bin/ticket
