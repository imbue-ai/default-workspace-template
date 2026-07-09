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

# NOTE: intentionally NOT guarded by the provisioning skip cache -- this produces
# in-repo outputs (frontend dist, .venv) that the create's git-mirror landing does
# not carry, so it must run on every create to regenerate them (fast via the baked
# warm caches). Only setup_system (global-only effects) is skipped.

# Disable OpenSSL CPU-cap detection. lima-VZ on Apple M5 advertises SVE in
# /proc/cpuinfo but traps the `cntb` SVE instruction OpenSSL emits during
# CPU-cap init -- so any cryptography>=47 import (mngr CLI, system-interface)
# SIGILLs in `_armv8_sve_get_vl_bytes`. OPENSSL_armcap=0 falls back to
# NEON-only paths, which run on both real M-series silicon and the VZ guest.
# The same env var rides the agent's runtime env via .mngr/settings.toml
# `host_env__extend`; this export covers the build-time `mngr plugin add`
# below, which runs before /mngr/env is sourced.
export OPENSSL_armcap=0

# Pin uv to a Python that satisfies the lockfile (>=3.12). The Docker base ships
# 3.12; on other bases setup_system.sh fetched a uv-managed 3.12, so point uv at
# it. No-op when system Python is already >=3.12 (Docker build unchanged).
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    export UV_PYTHON=3.12
fi

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
# mngr_codex/mngr_antigravity/mngr_opencode must be registered here too --
# without this, the `mngr` CLI tool itself (not just this repo's own uv
# workspace) can't parse .mngr/settings.toml's agent_types.codex/antigravity/
# opencode blocks (unknown-field error), since pluggy discovers a plugin's
# config schema from its registration with THIS tool, not from the
# workspace's own pyproject.toml dependency list. Caught by code review, not
# caught when those agent_types blocks were first added.
mngr plugin add \
    --path vendor/mngr/libs/mngr_claude \
    --path vendor/mngr/libs/mngr_codex \
    --path vendor/mngr/libs/mngr_antigravity \
    --path vendor/mngr/libs/mngr_opencode \
    --path vendor/mngr/libs/mngr_wait

# Sync the workspace venv (registers the editable workspace + path deps). --frozen
# asserts the lockfile is canonical so the pre-warmed cache is not bypassed.
uv sync --all-packages --frozen

# Expose the vendored tk ticket tracker on PATH. The target resolves once
# /mngr/code is in place (on docker, after the first-boot seed).
ln -sf "$REPO_ROOT/vendor/tk/ticket" /usr/local/bin/tk
ln -sf "$REPO_ROOT/vendor/tk/ticket" /usr/local/bin/ticket
