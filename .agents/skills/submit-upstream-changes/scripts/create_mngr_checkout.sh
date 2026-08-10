#!/usr/bin/env bash
set -euo pipefail
#
# create_mngr_checkout.sh
#
# Create a standalone mngr checkout at .external_worktrees/mngr for preparing
# an mngr PR (see ../references/mngr-changes.md for the full flow). Clones the
# mngr repo and creates a work branch off origin/main named after the current
# workspace branch, or the explicit argument. Refuses to leave the checkout on
# main: the checkout exists to assemble a PR branch, and the code-guardian
# stop hook reviews it with mngr's own committed policy (which merges and
# pushes the current branch), so it must never sit on the base branch.
#
# No .reviewer/settings.local.json is written: the checkout is a normal mngr
# clone and mngr's committed reviewer config applies as-is.
#
# Usage: create_mngr_checkout.sh [branch-name]
#
# MNGR_REMOTE_URL overrides the default remote.

url="${MNGR_REMOTE_URL:-git@github.com:imbue-ai/mngr-internal.git}"

repo_root=$(git rev-parse --show-toplevel)
dest="$repo_root/.external_worktrees/mngr"

if [ -e "$dest" ]; then
    echo "ERROR: $dest already exists" >&2
    exit 1
fi

branch="${1:-$(git -C "$repo_root" rev-parse --abbrev-ref HEAD)}"
if [ "$branch" = "main" ] || [ "$branch" = "master" ] || [ "$branch" = "HEAD" ]; then
    echo "ERROR: the workspace is on '$branch', which cannot name the mngr work branch." >&2
    echo "usage: $0 <branch-name>" >&2
    exit 1
fi

mkdir -p "$repo_root/.external_worktrees"
git clone "$url" "$dest"
git -C "$dest" checkout -q -b "$branch" origin/main

echo "mngr checkout ready: $dest on $branch"
