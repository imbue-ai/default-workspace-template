#!/usr/bin/env bash
# env.d unit: move the themes made in this workspace (themes/) onto theme contract 2.
#
# Contract 2 (docs/system/blueprint/brand-design-system/) renamed every color token: a theme made
# before it names tokens the workspace no longer reads, so it would be listed as unavailable and
# worn as the standard look. `workspace-themes migrate --all` rewrites each such theme's token
# names in place and moves its theme.toml to contract 2; a theme already there is left alone.
#
# The rewrite is committed on its own, so it can be undone in one step, with what the migration
# says to check about each theme in the message. A theme with changes the user had not committed
# is rewritten but not committed, so the commit never carries their unsaved edits.
#
# env.d contract: idempotent with a fast satisfied-check -- no markers.
set -euo pipefail

readonly REPO_ROOT="${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}"
readonly THEMES_DIR="$REPO_ROOT/themes"
readonly CONTRACT_1_LINE='^[[:space:]]*contract[[:space:]]*=[[:space:]]*1[[:space:]]*(#.*)?$'

# Satisfied-check: no workspace themes, or none still on contract 1.
if [ ! -d "$THEMES_DIR" ] || ! grep -lqsE "$CONTRACT_1_LINE" "$THEMES_DIR"/*/theme.toml; then
    echo "[env.d/migrate-workspace-themes] no contract-1 themes, satisfied"
    exit 0
fi

# The contract-1 themes whose folders hold nothing uncommitted (an untracked folder counts as
# uncommitted): only these are committed after the rewrite.
committable=()
if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    for manifest in "$THEMES_DIR"/*/theme.toml; do
        grep -qsE "$CONTRACT_1_LINE" "$manifest" || continue
        theme_id="$(basename "$(dirname "$manifest")")"
        if [ -z "$(git -C "$REPO_ROOT" status --porcelain -- "themes/$theme_id")" ]; then
            committable+=("$theme_id")
        else
            echo "[env.d/migrate-workspace-themes] themes/$theme_id has uncommitted changes; its rewrite is left uncommitted"
        fi
    done
fi

echo "[env.d/migrate-workspace-themes] moving contract-1 themes to contract 2..."
report="$(cd "$REPO_ROOT" && uv run --no-sync workspace-themes migrate --all)"
echo "$report"

if [ "${#committable[@]}" -eq 0 ]; then
    exit 0
fi
paths=()
details=""
for theme_id in "${committable[@]}"; do
    paths+=("themes/$theme_id")
    details+="$(awk -v prefix="$theme_id: " 'index($0, prefix) == 1' <<< "$report")"$'\n'
done
git -C "$REPO_ROOT" add -- "${paths[@]}"
if git -C "$REPO_ROOT" commit --quiet --no-verify \
    -m "Move themes to theme contract 2: ${committable[*]}" \
    -m "Rewritten by system/scripts/env.d/1300-migrate-workspace-themes.sh (workspace-themes migrate)." \
    -m "$details" -- "${paths[@]}"; then
    echo "[env.d/migrate-workspace-themes] committed the rewrite of: ${committable[*]}"
else
    echo "[env.d/migrate-workspace-themes] could not commit the rewrite; it is left uncommitted"
fi
