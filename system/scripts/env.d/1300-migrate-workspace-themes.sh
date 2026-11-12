#!/usr/bin/env bash
# env.d unit: move the themes made in this workspace (themes/) onto theme contract 2.
#
# Contract 2 (docs/system/blueprint/brand-design-system/) renamed every color token: a theme made
# before it names tokens the workspace no longer reads, so it would be listed as unavailable and
# worn as the standard look. `workspace-themes migrate --all` rewrites each such theme's token
# names in place and moves its theme.toml to contract 2; a theme already there is left alone.
# The themes are the user's, so nothing here commits them.
#
# env.d contract: idempotent with a fast satisfied-check -- no markers.
set -euo pipefail

readonly REPO_ROOT="${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}"
readonly THEMES_DIR="$REPO_ROOT/themes"

# Satisfied-check: no workspace themes, or none still on contract 1.
if [ ! -d "$THEMES_DIR" ] || ! grep -lqsE '^[[:space:]]*contract[[:space:]]*=[[:space:]]*1[[:space:]]*(#.*)?$' "$THEMES_DIR"/*/theme.toml; then
    echo "[env.d/migrate-workspace-themes] no contract-1 themes, satisfied"
    exit 0
fi

echo "[env.d/migrate-workspace-themes] moving contract-1 themes to contract 2..."
(cd "$REPO_ROOT" && uv run --no-sync workspace-themes migrate --all)
