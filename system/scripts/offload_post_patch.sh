#!/usr/bin/env bash
# Re-derives what a source change can invalidate, after offload has applied a
# run's thin diff onto the checkpoint image (its post_patch_cmd, see
# offload-modal*.toml at the repo root): the git index, the workspace venv and
# the built frontends. Dependency and toolchain changes never get here; the files that
# carry them are checkpoint build inputs, so they rebuild the image instead.
set -euo pipefail

cd /home/user/workspace
# offload applies the diff to the files alone, leaving the index at the
# checkpoint: without this, a file the diff adds is untracked and one it deletes
# is still listed, so tests that enumerate the tree with `git ls-files` see the
# checkpoint's file set instead of the run's.
git add -A
uv sync --all-packages --frozen
( cd system && npm run build )
