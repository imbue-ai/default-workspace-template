#!/bin/bash
# Install the pinned earlyoom static binary from imbue-ai/earlyoom, the fork
# that picks its victim by the kernel badness computed from oom_score_adj
# (upstream reads /proc/<pid>/oom_score, which gVisor serves as 0 for every
# process, so under gVisor it ignored the priority bands). Idempotent and
# version-gated like install_owner_exec.sh: a binary already at the pinned
# version is left in place.
#
# To bump: tag a new v1.9.0-imbue.N release on imbue-ai/earlyoom, then set
# EARLYOOM_VERSION and both sha256s below from its published .sha256 assets.
# The monorepo's `minds-admin hotpatch-earlyoom` pins the same version and
# x86_64 sha256 (apps/minds_admin/imbue/minds_admin/slices/earlyoom_hotpatch.py)
# and must move with it while that command still exists: it writes the same
# version stamp, so a hotpatched workspace whose stamp matches this pin
# downloads nothing here.
set -euo pipefail

EARLYOOM_VERSION="v1.9.0-imbue.1"
EARLYOOM_REPO="imbue-ai/earlyoom"
INSTALL_PATH="/usr/local/bin/earlyoom"
VERSION_STAMP="/usr/local/bin/.earlyoom-version"

# Already at the pinned version? Nothing to do.
if [ -x "$INSTALL_PATH" ] && [ "$(cat "$VERSION_STAMP" 2>/dev/null || true)" = "$EARLYOOM_VERSION" ]; then
    exit 0
fi

arch="$(uname -m)"
case "$arch" in
    x86_64)
        triple="x86_64-unknown-linux"
        sha256="d37fffbafd0612020a491235a3da50562e18e0656163145622e1d1859fdd66bf"
        ;;
    aarch64 | arm64)
        triple="aarch64-unknown-linux"
        sha256="b9fee4da601e22e1636ba1f12bba1336cabd893565122e29ebadac4b5f3cade2"
        ;;
    *) echo "install_earlyoom: unsupported arch $arch" >&2; exit 1 ;;
esac

asset="earlyoom-${triple}"
url="https://github.com/${EARLYOOM_REPO}/releases/download/${EARLYOOM_VERSION}/${asset}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --retry-all-errors: curl's --retry alone only covers "transient" failures
# (timeouts, 5xx), not protocol-level ones like HTTP/2 PROTOCOL_ERROR, which
# GitHub's CDN produces intermittently. Retrying on any error is safe here
# because the sha256 check below guards integrity.
curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors -o "${tmp}/${asset}" "$url"
echo "${sha256}  ${tmp}/${asset}" | sha256sum -c - >/dev/null

# install to a sibling, then rename: the running earlyoom keeps its inode, and
# supervisord's next start of it picks up the new binary.
install -m 0755 "${tmp}/${asset}" "${INSTALL_PATH}.new"
mv -f "${INSTALL_PATH}.new" "$INSTALL_PATH"
printf '%s\n' "$EARLYOOM_VERSION" > "$VERSION_STAMP"
