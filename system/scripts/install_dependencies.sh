#!/usr/bin/env bash
# Shared dependency install for default-workspace-template hosts.
#
# Installs third-party Python + Node dependencies from the lockfiles only (no
# workspace/local packages). Needs the dependency manifests present but not the
# full source, so the Dockerfile runs it right after copying the manifests (to
# preserve layer caching) and the Lima provider runs it after the repo is synced
# into the VM. Runs as root and is idempotent.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH="/root/.local/bin:$PATH"

# NOTE: intentionally NOT guarded by the provisioning skip cache -- this produces
# in-repo outputs (.venv, node_modules) that the create's git-mirror landing does
# not carry, so it must run on every create to regenerate them (fast via the
# baked warm uv/npm caches). Only setup_system (global-only effects) is skipped.

REPO_ROOT="${REPO_ROOT:-/home/user/workspace}"

# The lockfile's mngr packages may come from the private mngr repo; the uv sync
# below runs with the credential delivered to this build, if any, and nothing
# else here does (see _mngr_git_auth.sh).
. "$(dirname "$0")/_mngr_git_auth.sh"

# Python and JavaScript dependency installs are independent and could run in
# parallel; kept sequential for now (clarity), structured so parallelizing is a
# drop-in later.

# Pre-warm the uv wheel cache: install every third-party PyPI dep in the
# lockfile, skipping workspace + local path packages (build_workspace.sh
# registers those once the full source is present).
cd "$REPO_ROOT"
mngr_git_auth_run uv sync --all-packages --frozen --no-install-workspace --no-install-local

# Frontend npm dependencies (exact, from the lockfile): one npm workspace for every
# frontend (system/package.json lists the members) and their shared library.
# --no-audit: the audit is a per-install round trip to the registry that cannot
# affect the outcome here -- the tree is pinned by the lockfile, so nothing the
# audit reports changes what gets installed -- and when the registry's audit
# endpoint is slow that round trip stalls the install for minutes.
# --no-fund: funding output is noise in a build log.
cd "$REPO_ROOT/system"
npm ci --no-audit --no-fund
