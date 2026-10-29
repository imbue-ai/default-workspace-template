#!/bin/bash
#
# Antigravity CLI (agy) installer -- version-LOCKED to the AGY_VERSION below.
#
# A vendored, pinned fork of Google's upstream bootstrapper
# (https://antigravity.google/cli/install.sh). The upstream script queries a
# "latest" manifest and always installs the newest build, which is not
# reproducible. This copy hardcodes one release's URL + sha512 per arch
# (captured from the manifest and verified), so every image bakes the exact same
# agy build. Bump by re-fetching the manifests
# (https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_{amd64,arm64}.json)
# and replacing the AGY_VERSION / URL / SHA512 values below.
#
# Differences from upstream, all deliberate:
#   * no manifest query -- the release info is pinned inline;
#   * no trailing `agy install` shell-env handoff (PATH is managed by the image);
#   * glibc linux amd64/arm64 only (the platforms this template builds on).
#
set -euo pipefail

AGY_VERSION="1.4.7"
TARGET_DIR="${1:-$HOME/.local/bin}"
BINARY_PATH="$TARGET_DIR/agy"

# Pinned release, per glibc-linux arch (url + sha512 from the manifest).
case "$(uname -m)" in
    x86_64|amd64)
        url="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.7-5900205422411776/linux-x64/cli_linux_x64.tar.gz"
        sha512="beda760587589f5827b0b516c3ec2c5bd5cd2e91eb0e2533807ba1e990a27dcd63aef0191e0b76fb60feab0d16eaa8210fb79842c34dd4809e257e1712edd152"
        ;;
    arm64|aarch64)
        url="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.7-5900205422411776/linux-arm/cli_linux_arm64.tar.gz"
        sha512="6a9fa0df6300633404571f66ec7924098cc70a9f23ac55c3659139bbcbdd2ea5301fe1ef728d4c064fac65104ea14a2b74421f320a82e5bcf218c482b58e3a16"
        ;;
    *)
        echo "Fatal: agy pin ${AGY_VERSION} covers only glibc linux amd64/arm64, got $(uname -m)." >&2
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
