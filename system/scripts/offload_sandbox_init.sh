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
readonly OFFLOAD_VERSION="0.10.0"
readonly REPO_ROOT="/home/user/workspace"

# An imbue_cloud workspace's /home/user is a symlink onto the host volume's home
# (mngr_vps's container setup), not the directory the image ships, so the kernel
# and claude report its paths resolved. Give the tests that layout too.
readonly VOLUME_HOME="/mngr-vol/home"
if [ ! -L /home/user ]; then
    mkdir -p "$(dirname "$VOLUME_HOME")"
    mv /home/user "$VOLUME_HOME"
    ln -s "$VOLUME_HOME" /home/user
fi

cd "$REPO_ROOT"

# The build context reaches Modal through its file upload, which copies what a
# symlink points at in place of the link and leaves out a link whose target is
# not in the context (docker's COPY keeps both as links). The index in the
# exported checkout still records every link, so put them back from there.
git ls-files -s | awk -F'\t' '$1 ~ /^120000/ { print $2 }' > /tmp/offload-symlinks
git checkout-index -f --stdin < /tmp/offload-symlinks
rm /tmp/offload-symlinks

# The Screen's stack (Fortress included), which a workspace installs on first boot rather than in the image.
ENV_CONVERGE_WORKSPACE_DIR="$REPO_ROOT" bash "$REPO_ROOT/system/scripts/env.d/2000-screen.sh"

# offload has no prebuilt binary, so build it with a toolchain that is removed again afterwards.
readonly BUILD_HOME="/opt/offload-build"
export CARGO_HOME="$BUILD_HOME/cargo"
export RUSTUP_HOME="$BUILD_HOME/rustup"
curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
"$CARGO_HOME/bin/cargo" install "offload@$OFFLOAD_VERSION" --locked --root "$BUILD_HOME/offload"
install -m 0755 "$BUILD_HOME/offload/bin/offload" /usr/local/bin/offload
rm -rf "$BUILD_HOME"
