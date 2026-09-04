#!/usr/bin/env bash
# Fetch the few non-Python files this workspace needs from mngr, at the commit
# pyproject.toml pins, into system/vendor/mngr-assets/ (gitignored).
#
# mngr itself is installed as Python packages from that same commit; these are
# the two things a package cannot deliver:
#   apps/minds/imbue/minds/desktop_client/static/   the embed contract and the
#                                                   service icons the system_interface
#                                                   frontend bundles at build time
#   libs/mngr_ttyd/imbue/mngr_ttyd/resources/       the OSC 52-capable ttyd client
#                                                   the terminal app serves
#
# A sparse, blob-filtered fetch pulls only those paths. The result carries a
# .commit marker, so re-running at the same pin is a no-op.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ASSETS_DIR="$REPO_ROOT/system/vendor/mngr-assets"
ASSET_PATHS=(
    apps/minds/imbue/minds/desktop_client/static
    libs/mngr_ttyd/imbue/mngr_ttyd/resources
)

read -r GIT_URL REV < <(python3 "$REPO_ROOT/system/scripts/list_mngr_plugins.py" --pin --repo-root "$REPO_ROOT")

if [ -f "$ASSETS_DIR/.commit" ] && [ "$(cat "$ASSETS_DIR/.commit")" = "$REV" ]; then
    exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git -C "$work" init -q
git -C "$work" remote add origin "$GIT_URL"
git -C "$work" sparse-checkout set --no-cone "${ASSET_PATHS[@]}"
git -C "$work" fetch -q --depth=1 --filter=blob:none origin "$REV"
git -C "$work" checkout -q FETCH_HEAD

staging="$ASSETS_DIR.tmp"
rm -rf "$staging"
mkdir -p "$staging"
for path in "${ASSET_PATHS[@]}"; do
    mkdir -p "$staging/$(dirname "$path")"
    cp -R "$work/$path" "$staging/$path"
done
printf '%s\n' "$REV" > "$staging/.commit"
rm -rf "$ASSETS_DIR"
mv "$staging" "$ASSETS_DIR"
echo "fetched mngr assets at ${REV:0:10} into ${ASSETS_DIR#"$REPO_ROOT"/}"
