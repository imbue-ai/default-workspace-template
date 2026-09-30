#!/usr/bin/env bash
# Re-derives what a source change can invalidate, after offload has applied a
# run's thin diff onto the checkpoint image (its post_patch_cmd, see
# offload-modal*.toml at the repo root): the workspace venv and the built
# frontends. Dependency and toolchain changes never get here; the files that
# carry them are checkpoint build inputs, so they rebuild the image instead.
set -euo pipefail

cd /home/user/workspace
uv sync --all-packages --frozen
( cd system && npm run build )
