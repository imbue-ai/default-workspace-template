#!/usr/bin/env bash
# Build the vendored harbor viewer into viewer/build/client, bootstrapping bun on first use.
# `just minds-evals-view` calls this before serving; running it directly is equivalent.
#
# bun rather than npm or pnpm: upstream's committed package-lock.json is out of sync with its
# package.json, so `npm ci` refuses outright, and `npm install` resolves a different dependency
# set. bun.lock is the lockfile upstream actually maintains, so it is the one that reproduces
# the dependency tree harbor builds against.
set -euo pipefail

# A floor, not a pin. The JavaScript dependency tree is pinned by bun.lock and `--frozen-lockfile`
# below, so bun's own version only has to be new enough to read that lockfile and run the build.
BUN_MINIMUM_VERSION="1.4.0"

evals_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
viewer_dir="$evals_dir/viewer"
# Kept inside the project, not on the developer's machine: bun is needed only by the viewer
# recipes, and .bun/ is gitignored alongside the build output it produces.
bun_root="$evals_dir/.bun"
bun="$bun_root/bin/bun"

# Returns success when $1 is at least BUN_MINIMUM_VERSION, by version order rather than string
# order, so 1.10 counts as newer than 1.4.
is_new_enough() {
  [ "$(printf '%s\n%s\n' "$BUN_MINIMUM_VERSION" "$1" | sort -V | head -n1)" = "$BUN_MINIMUM_VERSION" ]
}

if [ ! -x "$bun" ]; then
  system_bun="$(command -v bun || true)"
  if [ -n "$system_bun" ] && is_new_enough "$("$system_bun" --version)"; then
    bun="$system_bun"
  else
    echo "Installing bun into $bun_root ..."
    installer="$(mktemp)"
    trap 'rm -f "$installer"' EXIT
    curl -fsSL https://bun.sh/install -o "$installer"
    # SHELL is deliberately not a shell the installer recognises. Every branch of its `case
    # $(basename "$SHELL")` appends a PATH block to the matching rc file and runs `bun
    # completions`; the fallback branch only prints what it would have added. This bun belongs to
    # the project, so it has no business editing the developer's shell configuration.
    SHELL=none BUN_INSTALL="$bun_root" bash "$installer"
  fi
fi

marker="$viewer_dir/build/client/index.html"
sources=("$viewer_dir/app" "$viewer_dir/package.json" "$viewer_dir/bun.lock" "$viewer_dir/vite.config.ts" "$viewer_dir/react-router.config.ts")
if [ -f "$marker" ] && [ -z "$(find "${sources[@]}" -newer "$marker" -print -quit)" ]; then
  echo "Viewer build is up to date."
  exit 0
fi

cd "$viewer_dir"
"$bun" install --frozen-lockfile
"$bun" run build
echo "Built $viewer_dir/build/client"
