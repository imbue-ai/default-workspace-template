#!/bin/bash
#
# Antigravity CLI (agy) installer -- version-LOCKED to 1.1.16.
#
# A vendored, pinned fork of Google's upstream bootstrapper
# (https://antigravity.google/cli/install.sh). The upstream script queries a
# "latest" manifest and always installs the newest build, which is not
# reproducible. This copy hardcodes the 1.1.16 release URL + sha512 per arch
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

AGY_VERSION="1.1.16"
TARGET_DIR="${1:-$HOME/.local/bin}"
BINARY_PATH="$TARGET_DIR/agy"

# Pinned 1.1.16 release, per glibc-linux arch (url + sha512 from the manifest).
case "$(uname -m)" in
    x86_64|amd64)
        url="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.1.16-6607970839166976/linux-x64/cli_linux_x64.tar.gz"
        sha512="7354ef5af32b7ce784ac6410aa007544040a33a880b505fbbacb73b416058212d9bf48c597ccabc88cfdbc2ac0e0550b4b6b13acff19b6df39de16a02e8ba00e"
        ;;
    arm64|aarch64)
        url="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.1.16-6607970839166976/linux-arm/cli_linux_arm64.tar.gz"
        sha512="66e6de43af57a6b055b4c2f0b3428d9ffea5d5daa122295911b40d3fc8b5e950fc3468aee5d239752bc884f08b345b45c550d06266a96c3d08addb6c7e7e06ba"
        ;;
    *)
        echo "Fatal: agy pin 1.1.16 covers only glibc linux amd64/arm64, got $(uname -m)." >&2
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
