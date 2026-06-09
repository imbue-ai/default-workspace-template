#!/usr/bin/env bash
# Shared dependency install for forever-claude-template hosts.
#
# Installs third-party Python + Node dependencies from the lockfiles only (no
# workspace/local packages). Needs the dependency manifests present but not the
# full source, so the Dockerfile runs it right after copying the manifests (to
# preserve layer caching) and the Lima provider runs it after the repo is synced
# into the VM. Runs as root and is idempotent.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH="/root/.local/bin:$PATH"

REPO_ROOT="${REPO_ROOT:-/mngr/code}"

# Python and JavaScript dependency installs are independent and could run in
# parallel; kept sequential for now (clarity), structured so parallelizing is a
# drop-in later.

# Pre-warm the uv wheel cache: install every third-party PyPI dep in the
# lockfile, skipping workspace + local path packages (build_workspace.sh
# registers those once the full source is present).
cd "$REPO_ROOT"
uv sync --all-packages --frozen --no-install-workspace --no-install-local

# Frontend npm dependencies (exact, from the lockfile).
cd "$REPO_ROOT/apps/system_interface/frontend"
npm ci
