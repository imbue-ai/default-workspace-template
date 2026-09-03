#!/usr/bin/env bash
# Shared workspace build for default-workspace-template hosts.
#
# Builds the workspace from full source: builds the frontend, installs the mngr
# tool and one tool per Python app (with their mngr plugins), registers the
# editable workspace + vendored mngr packages, and exposes the tk ticket tracker. Needs the full repo
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
# below, which runs before /home/user/.mngr/env is sourced.
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

# Build the system_interface frontend (deps installed by install_dependencies.sh).
( cd "$REPO_ROOT/system/apps/system_interface/frontend" && npm run build )

# Install mngr as a tool, then every Python app (each system/apps/<package>/
# with both a pyproject.toml and an app.toml manifest) as its own tool from its
# own pyproject, so no app runs from the root venv and one app's pins never
# constrain another's. The manifest is the discriminator: an app with a
# pyproject but no manifest was scaffolded before manifests existed, still runs
# `uv run <name>` from the root venv, and is left to the migration. An app's
# tool also gets the mngr plugins system/config/mngr_plugins.toml assigns to
# its manifest name, as editable extras, so it can parse plugin-specific
# config; the update-self apply reads the same table, so a release adding a
# plugin registers it in existing workspaces as well as here. mngr_modal is
# intentionally not registered (providers.modal.is_enabled=false).
MNGR_PLUGIN_ARGS=()
while IFS= read -r plugin_path; do
    MNGR_PLUGIN_ARGS+=(--path "$plugin_path")
done < <(python3 "$REPO_ROOT/system/scripts/list_mngr_plugins.py" --tool mngr --repo-root "$REPO_ROOT")

uv tool install -e "$REPO_ROOT/system/vendor/mngr/libs/mngr"

for app_dir in "$REPO_ROOT"/system/apps/*/; do
    [ -f "$app_dir/pyproject.toml" ] && [ -f "$app_dir/app.toml" ] || continue
    app_name="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["name"])' "$app_dir/app.toml")"
    APP_PLUGIN_ARGS=()
    while IFS= read -r plugin_path; do
        APP_PLUGIN_ARGS+=(--with-editable "$REPO_ROOT/$plugin_path")
    done < <(python3 "$REPO_ROOT/system/scripts/list_mngr_plugins.py" --tool "$app_name" --repo-root "$REPO_ROOT")
    uv tool install -e "$app_dir" "${APP_PLUGIN_ARGS[@]}"
done

mngr plugin add "${MNGR_PLUGIN_ARGS[@]}"

# Sync the workspace venv (registers the editable workspace + path deps). --frozen
# asserts the lockfile is canonical so the pre-warmed cache is not bypassed.
uv sync --all-packages --frozen

# Expose the vendored tk ticket tracker on PATH. The target resolves once
# /home/user/workspace is in place (on docker, after the first-boot seed).
ln -sf "$REPO_ROOT/system/vendor/tk/ticket" /usr/local/bin/tk
ln -sf "$REPO_ROOT/system/vendor/tk/ticket" /usr/local/bin/ticket
