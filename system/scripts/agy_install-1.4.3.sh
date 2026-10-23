#!/bin/bash
#
# Antigravity CLI (agy) installer -- version-LOCKED to 1.4.3.
#
# A vendored, pinned fork of Google's upstream bootstrapper
# (https://antigravity.google/cli/install.sh). The upstream script queries a
# "latest" manifest and always installs the newest build, which is not
# reproducible. This copy hardcodes the 1.4.3 release URL + sha512 per arch
# (captured from the manifest and verified), so every image bakes the exact same
# agy build. Bump by re-fetching the manifest for the new version and replacing
# the AGY_VERSION / URL / SHA512 values below.
#
# Differences from upstream, all deliberate:
#   * no manifest query -- the release info is pinned inline;
#   * no trailing `agy install` shell-env handoff (PATH is managed by the image);
#   * glibc linux amd64/arm64 only (the platforms this template builds on).
#
set -euo pipefail

AGY_VERSION="1.4.3"
TARGET_DIR="${1:-$HOME/.local/bin}"
BINARY_PATH="$TARGET_DIR/agy"

# Pinned 1.4.3 release, per glibc-linux arch (url + sha512 from the manifest).
case "$(uname -m)" in
    x86_64|amd64)
        url="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.3-4577820882632704/linux-x64/cli_linux_x64.tar.gz"
        sha512="9b726b86280a56e8ffa84b3b3a1c00d19972833a1f6c0b21d85c5fc6e408af6492e4c61353cad9b500bee599af264c92cfc2ead1f5048ab9525328e1e4199c40"
        ;;
    arm64|aarch64)
        url="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.3-4577820882632704/linux-arm/cli_linux_arm64.tar.gz"
        sha512="a906ef290e077871b333cb6f13d4d76ccdfc03ad15f012d6b745c02fa471edc27c10bc3059b87142bf6de0bacd64328f62e608d5736655524b97e8d4b4a0882a"
        ;;
    *)
        echo "Fatal: agy pin 1.4.3 covers only glibc linux amd64/arm64, got $(uname -m)." >&2
        exit 1
        ;;
esac

# Download to a staging dir, sha512-verify, extract the `antigravity` member, and
# install it as `agy`. rename(2) into place so re-provisioning a live host with a
# running agy is safe (the same reason the other installers in setup_system use mv).
staging_dir="$(mktemp -d)"
trap 'rm -rf "$staging_dir"' EXIT

echo "Downloading agy ${AGY_VERSION}..."
curl -fsSL "$url" -o "$staging_dir/agy.tar.gz"

actual_sha512="$(sha512sum "$staging_dir/agy.tar.gz" | cut -d' ' -f1)"
if [ "$actual_sha512" != "$sha512" ]; then
    echo "Security halt: agy ${AGY_VERSION} checksum mismatch (expected $sha512, got $actual_sha512)." >&2
    exit 1
fi

tar -xzf "$staging_dir/agy.tar.gz" -C "$staging_dir" antigravity

mkdir -p "$TARGET_DIR"
chmod 0755 "$staging_dir/antigravity"
mv -f "$staging_dir/antigravity" "$BINARY_PATH"
echo "Installed agy ${AGY_VERSION} to ${BINARY_PATH}."
