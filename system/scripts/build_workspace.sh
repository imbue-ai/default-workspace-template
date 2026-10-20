#!/usr/bin/env bash
# Shared workspace build for default-workspace-template hosts.
#
# Builds the workspace from full source: builds the frontend, installs the mngr
# tool (with its plugins), syncs the workspace venv every Python app and service
# runs from (the editable workspace + the pinned mngr packages), and exposes the
# tk ticket tracker. Needs the full repo
# present, so the Dockerfile runs it after copying all source and the Lima
# provider runs it after the repo is synced into the VM. Runs as root and is
# idempotent.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Pin the uv tool directories and put their bin dir first on PATH, so the installs
# below land in the environment every later lookup resolves -- whichever $HOME this
# script happens to run under (see _tool_env.sh).
. "$(dirname "$0")/_tool_env.sh"
tool_env_pin

# The pin may name the private mngr repo; each uv command below that fetches
# mngr runs with the credential delivered to this build, if any, and nothing
# else does (see _mngr_git_auth.sh). The asset fetch the frontend build runs
# scopes its own git fetch the same way.
. "$(dirname "$0")/_mngr_git_auth.sh"

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
# `host_env__extend`; this export covers the build-time installs below, which
# run before /home/user/.mngr/env is sourced.
export OPENSSL_armcap=0

# Pin uv to a Python that satisfies the lockfile (>=3.12). The Docker base ships
# 3.12; on other bases setup_system.sh fetched a uv-managed 3.12, so point uv at
# it. No-op when system Python is already >=3.12 (Docker build unchanged).
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    export UV_PYTHON=3.12
fi

REPO_ROOT="${REPO_ROOT:-/home/user/workspace}"
cd "$REPO_ROOT"

# Mark the repo a git safe.directory so in-container/in-VM git commands don't
# refuse on an ownership mismatch.
git config --global --add safe.directory "$REPO_ROOT"

# Build every frontend of the npm workspace (deps installed by install_dependencies.sh;
# system/package.json lists the members), each into the static/ directory its backend serves.
( cd "$REPO_ROOT/system" && npm run build )

# Install mngr as a tool, with the mngr plugins system/config/mngr_plugins.toml
# assigns it as extras, so it can parse plugin-specific config. mngr and every
# plugin come from the public-repo commit pyproject.toml's [tool.uv.sources] pins
# imbue-mngr to. The update-self apply reads the same table, so a release adding
# a plugin registers it in existing workspaces as well as here. mngr_modal is
# intentionally not registered (providers.modal.is_enabled=false).
mngr_git_auth_run python3 "$REPO_ROOT/system/scripts/install_mngr.py" --repo-root "$REPO_ROOT"

# Every Python app runs from the workspace venv the sync below writes, its only
# install. Drop any per-app uv tool an older image still carries (see tool_env.py).
python3 "$REPO_ROOT/system/scripts/tool_env.py" drop-app-tools --repo-root "$REPO_ROOT"

# Drop an mngr install left under a different $HOME by a create that ran before the pin
# above: it shadows the pinned one on every login shell's PATH and no update refreshes it.
# Shared with the update apply, which does the same for workspaces whose update can still
# run -- see tool_env.py. Runs after the install, so it always has a confirmed copy to keep.
python3 "$REPO_ROOT/system/scripts/tool_env.py" drop-shadowing-mngr

# Sync the workspace venv (registers the editable workspace + path deps). --frozen
# asserts the lockfile is canonical so the pre-warmed cache is not bypassed.
mngr_git_auth_run uv sync --all-packages --frozen

# Expose the vendored tk ticket tracker on PATH. The target resolves once
# /home/user/workspace is in place (on docker, after the first-boot seed).
ln -sf "$REPO_ROOT/system/vendor/tk/ticket" /usr/local/bin/tk
ln -sf "$REPO_ROOT/system/vendor/tk/ticket" /usr/local/bin/ticket
