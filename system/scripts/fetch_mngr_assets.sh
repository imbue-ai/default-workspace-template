#!/usr/bin/env bash
# Fetch the few non-Python files this workspace needs from mngr, from the source
# pyproject.toml gives it, into system/vendor/mngr-assets/ (gitignored).
#
# mngr itself is installed as Python packages from that same source; these are
# the two things a package cannot deliver:
#   apps/minds/imbue/minds/desktop_client/static/   the embed contract and the
#                                                   service icons the system_interface
#                                                   frontend bundles at build time
#   libs/mngr_ttyd/imbue/mngr_ttyd/resources/       the OSC 52-capable ttyd client
#                                                   the terminal app serves
#
# At a git pin, a sparse, blob-filtered fetch pulls only those paths and the result
# carries a .commit marker, so re-running at the same pin is a no-op. With a local
# mngr tree (system/vendor/mngr, while developing against a checkout) they are
# copied from it every time, since that tree changes without a commit.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ASSETS_DIR="$REPO_ROOT/system/vendor/mngr-assets"
ASSET_PATHS=(
    apps/minds/imbue/minds/desktop_client/static
    libs/mngr_ttyd/imbue/mngr_ttyd/resources
)

read -r GIT_URL REV < <(python3 "$REPO_ROOT/system/scripts/list_mngr_plugins.py" --pin --repo-root "$REPO_ROOT")

if [ -z "${REV:-}" ]; then
    # A local tree prints a single path.
    source_tree="$GIT_URL"
    marker="local"
else
    if [ -f "$ASSETS_DIR/.commit" ] && [ "$(cat "$ASSETS_DIR/.commit")" = "$REV" ]; then
        exit 0
    fi
    source_tree="$(mktemp -d)"
    trap 'rm -rf "$source_tree"' EXIT
    git -C "$source_tree" init -q
    git -C "$source_tree" remote add origin "$GIT_URL"
    git -C "$source_tree" sparse-checkout set --no-cone "${ASSET_PATHS[@]}"
    git -C "$source_tree" fetch -q --depth=1 --filter=blob:none origin "$REV"
    git -C "$source_tree" checkout -q FETCH_HEAD
    marker="$REV"
fi

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
