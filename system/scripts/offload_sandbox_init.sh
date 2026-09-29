#!/usr/bin/env bash
# What a workspace's first boot adds to the product image, so offload's checkpoint
# image is a booted workspace rather than the image as shipped. offload runs this
# once per checkpoint build (its sandbox_init_cmd, see offload-modal*.toml at the
# repo root), after default-workspace-template-seed has put the tree at
# /home/user/workspace; every later run rides the cached result as a thin source
# diff. Nothing here runs at test time.
set -euo pipefail

# Keep in lockstep with the version ci.yml installs on the runner: the two talk
# to each other (offload applies each run's source diff inside the image with
# this binary).
readonly OFFLOAD_VERSION="0.9.14"
readonly REPO_ROOT="/home/user/workspace"

cd "$REPO_ROOT"

# The browser stack, which a workspace installs on first boot rather than in the image.
ENV_CONVERGE_WORKSPACE_DIR="$REPO_ROOT" bash "$REPO_ROOT/system/scripts/env.d/1000-playwright-fortress.sh"

# offload has no prebuilt binary, so build it with a toolchain that is removed again afterwards.
readonly BUILD_HOME="/opt/offload-build"
export CARGO_HOME="$BUILD_HOME/cargo"
export RUSTUP_HOME="$BUILD_HOME/rustup"
curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
"$CARGO_HOME/bin/cargo" install "offload@$OFFLOAD_VERSION" --locked --root "$BUILD_HOME/offload"
install -m 0755 "$BUILD_HOME/offload/bin/offload" /usr/local/bin/offload
rm -rf "$BUILD_HOME"
