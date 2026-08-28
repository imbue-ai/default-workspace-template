#!/bin/bash
# Install the pinned dufs static binary (the workspace's file-viewer server,
# https://github.com/sigoden/dufs) from its GitHub release. Idempotent and
# version-gated like install_owner_exec.sh: a binary already at the pinned
# version is left in place.
#
# dufs publishes no checksum assets, so the per-arch tarball sha256s are pinned
# here, recorded from the release at pin time; a version bump must update both.
#
# A version bump must ALSO re-vendor system/apps/files/assets/ from the new
# tag and re-apply its marked "minds patch" blocks (see that directory's
# README): the vendored frontend is served via --assets and must come from the
# same release as the binary.
set -euo pipefail

DUFS_VERSION="v0.46.0"
INSTALL_PATH="/usr/local/bin/dufs"
VERSION_STAMP="/usr/local/bin/.dufs-version"

# Already at the pinned version? Nothing to do.
if [ -x "$INSTALL_PATH" ] && [ "$(cat "$VERSION_STAMP" 2>/dev/null || true)" = "$DUFS_VERSION" ]; then
    exit 0
fi

arch="$(uname -m)"
case "$arch" in
    x86_64)
        triple="x86_64-unknown-linux-musl"
        sha256="817769f726613194bcff9d0e3e481eaccc86ac11208857614f36a8c02f410977"
        ;;
    aarch64 | arm64)
        triple="aarch64-unknown-linux-musl"
        sha256="1472123ae3aa07e49404d16b20305c2dec90c59883ebda9308717f7205e6511b"
        ;;
    *) echo "install_dufs: unsupported arch $arch" >&2; exit 1 ;;
esac

asset="dufs-${DUFS_VERSION}-${triple}.tar.gz"
url="https://github.com/sigoden/dufs/releases/download/${DUFS_VERSION}/${asset}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --retry-all-errors: curl's --retry alone only covers "transient" failures
# (timeouts, 5xx), not protocol-level ones like HTTP/2 PROTOCOL_ERROR, which
# GitHub's CDN produces intermittently. Retrying on any error is safe here
# because the sha256 check below guards integrity.
curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors -o "${tmp}/${asset}" "$url"
echo "${sha256}  ${tmp}/${asset}" | sha256sum -c - >/dev/null

tar -xzf "${tmp}/${asset}" -C "$tmp" dufs
install -m 0755 "${tmp}/dufs" "${INSTALL_PATH}.new"
mv -f "${INSTALL_PATH}.new" "$INSTALL_PATH"
printf '%s\n' "$DUFS_VERSION" > "$VERSION_STAMP"
