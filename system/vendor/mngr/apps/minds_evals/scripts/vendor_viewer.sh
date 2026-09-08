#!/usr/bin/env bash
# Replace apps/minds_evals/viewer/ with a fresh copy of harbor's apps/viewer at a given tag, or
# report how far the current tree has drifted from one.
#
#   vendor_viewer.sh v0.22.0           re-vendor at that tag
#   vendor_viewer.sh --diff v0.22.0    show our local delta against it
#
# The tag must match the harbor pin in apps/minds_evals/pyproject.toml: the viewer talks to
# harbor's own API, and a newer frontend calls endpoints an older backend does not serve.
set -euo pipefail

UPSTREAM_REPO="harbor-framework/harbor"
UPSTREAM_PATH="apps/viewer"
# Upstream files dropped on the way in. Claude Code reads every CLAUDE.md beneath the checkout as
# project instructions, and upstream's describes harbor's own repository -- `harbor view --dev`,
# `--build`, a bun invoked from a different root -- so left in place it instructs agents working
# here to run commands that do not apply. VENDORED_FROM.md says what to do instead.
PRUNE=("CLAUDE.md")

viewer_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/viewer"
provenance="$viewer_dir/VENDORED_FROM.md"

usage() {
  echo "usage: $(basename "$0") [--diff] <tag>" >&2
  exit 2
}

mode="vendor"
if [ "${1:-}" = "--diff" ]; then
  mode="diff"
  shift
fi
tag="${1:-}"
[ -n "$tag" ] || usage

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Fetching $UPSTREAM_REPO at $tag ..."
curl -fsSL "https://codeload.github.com/$UPSTREAM_REPO/tar.gz/refs/tags/$tag" -o "$work/src.tar.gz"
mkdir -p "$work/src"
tar xzf "$work/src.tar.gz" -C "$work/src" --strip-components=1
upstream="$work/src/$UPSTREAM_PATH"
[ -d "$upstream" ] || { echo "error: $UPSTREAM_PATH is absent from $UPSTREAM_REPO at $tag" >&2; exit 1; }

for pruned in "${PRUNE[@]}"; do
  rm -f "$upstream/$pruned"
done

if [ "$mode" = "diff" ]; then
  # The provenance file is ours, so it is never part of the delta. The rest are gitignored build
  # outputs, installed packages, and generated types, all of which may be present in a working tree.
  diff -r -x VENDORED_FROM.md -x node_modules -x build -x .react-router "$upstream" "$viewer_dir" && {
    echo "No local changes against $tag."
    exit 0
  }
  exit 1
fi

# The API answers with the commit a tag resolves to, which is what uv.lock records for the
# harbor pin and therefore the identifier worth writing down.
commit="$(curl -fsSL "https://api.github.com/repos/$UPSTREAM_REPO/commits/$tag" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"

# The provenance file is ours, not upstream's, so it survives the replacement and only its tag
# and commit rows are rewritten.
cp "$provenance" "$work/VENDORED_FROM.md"
rm -rf "$viewer_dir"
cp -R "$upstream" "$viewer_dir"
TAG="$tag" COMMIT="$commit" python3 - "$work/VENDORED_FROM.md" "$provenance" <<'PY'
import os
import re
import sys

source, destination = sys.argv[1], sys.argv[2]
text = open(source).read()
text = re.sub(r"^\| Tag \| .*$", f"| Tag | `{os.environ['TAG']}` |", text, count=1, flags=re.M)
text = re.sub(r"^\| Commit \| .*$", f"| Commit | `{os.environ['COMMIT']}` |", text, count=1, flags=re.M)
open(destination, "w").write(text)
PY

echo "Vendored $UPSTREAM_REPO $UPSTREAM_PATH at $tag ($commit) into $viewer_dir"
echo "Review 'git diff' -- it now shows upstream's changes and any local edits this replaced."
