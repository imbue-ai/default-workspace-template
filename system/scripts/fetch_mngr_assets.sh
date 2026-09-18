#!/usr/bin/env bash
# Fetch the few non-Python files this workspace needs from mngr, from the source
# pyproject.toml gives it, into system/vendor/mngr-assets/ (gitignored).
#
# mngr itself is installed as Python packages from that same source; these are
# the files no published package carries:
#   apps/minds/imbue/minds/desktop_client/static/   the embed contract and the
#                                                   service icons the frontends
#                                                   bundle at build time
#   style_guide.md                                  the base code style guide,
#                                                   docs/system/style_guide.md
#
# A sparse, blob-filtered fetch pulls only those paths and the result carries a
# .commit marker naming the pin and the asset list, so re-running with both
# unchanged is a no-op.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ASSETS_DIR="$REPO_ROOT/system/vendor/mngr-assets"
ASSET_PATHS=(
    apps/minds/imbue/minds/desktop_client/static
    style_guide.md
)

read -r GIT_URL REV < <(python3 "$REPO_ROOT/system/scripts/list_mngr_plugins.py" --pin --repo-root "$REPO_ROOT")
asset_list_hash="$(printf '%s\n' "${ASSET_PATHS[@]}" | shasum | cut -c1-12)"

marker="$REV $asset_list_hash"
if [ -f "$ASSETS_DIR/.commit" ] && [ "$(cat "$ASSETS_DIR/.commit")" = "$marker" ]; then
    exit 0
fi
source_tree="$(mktemp -d)"
trap 'rm -rf "$source_tree"' EXIT
git -C "$source_tree" init -q
git -C "$source_tree" remote add origin "$GIT_URL"
git -C "$source_tree" sparse-checkout set --no-cone "${ASSET_PATHS[@]}"
git -C "$source_tree" fetch -q --depth=1 --filter=blob:none origin "$REV"
git -C "$source_tree" checkout -q FETCH_HEAD

staging="$ASSETS_DIR.tmp"
rm -rf "$staging"
mkdir -p "$staging"
for path in "${ASSET_PATHS[@]}"; do
    mkdir -p "$staging/$(dirname "$path")"
    cp -R "$source_tree/$path" "$staging/$path"
done
printf '%s\n' "$marker" > "$staging/.commit"
rm -rf "$ASSETS_DIR"
mv "$staging" "$ASSETS_DIR"
echo "fetched mngr assets at ${marker:0:10} into ${ASSETS_DIR#"$REPO_ROOT"/}"
