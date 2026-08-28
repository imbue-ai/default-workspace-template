#!/bin/bash
# Install the pinned owner-exec static binary from its GitHub release, verifying
# the published sha256. Idempotent and version-gated: a binary already at the
# pinned version is left in place. Runs both at image build (inner service) and
# is the same logic the VM install path mirrors from the monorepo.
#
# The version is pinned here and MUST be kept in lockstep with the monorepo's
# VM-install pin (see the bump-owner-exec skill).
set -euo pipefail

OWNER_EXEC_VERSION="v0.2.1"
OWNER_EXEC_REPO="imbue-ai/owner-exec"
INSTALL_PATH="/usr/local/bin/owner-exec"
VERSION_STAMP="/usr/local/bin/.owner-exec-version"

# Already at the pinned version? Nothing to do.
if [ -x "$INSTALL_PATH" ] && [ "$(cat "$VERSION_STAMP" 2>/dev/null || true)" = "$OWNER_EXEC_VERSION" ]; then
    exit 0
fi

arch="$(uname -m)"
case "$arch" in
    x86_64) triple="x86_64-unknown-linux" ;;
    aarch64 | arm64) triple="aarch64-unknown-linux" ;;
    *) echo "install_owner_exec: unsupported arch $arch" >&2; exit 1 ;;
esac

asset="owner-exec-${triple}"
base_url="https://github.com/${OWNER_EXEC_REPO}/releases/download/${OWNER_EXEC_VERSION}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --retry-all-errors: curl's --retry alone only covers "transient" failures
# (timeouts, 5xx), not protocol-level ones like HTTP/2 PROTOCOL_ERROR (exit 92),
# which GitHub's CDN produces intermittently. Retrying on any error is safe here
# because the sha256 check below guards integrity.
curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors -o "${tmp}/${asset}" "${base_url}/${asset}"
curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors -o "${tmp}/${asset}.sha256" "${base_url}/${asset}.sha256"
( cd "$tmp" && sha256sum -c "${asset}.sha256" >/dev/null )

install -m 0755 "${tmp}/${asset}" "${INSTALL_PATH}.new"
mv -f "${INSTALL_PATH}.new" "$INSTALL_PATH"
printf '%s\n' "$OWNER_EXEC_VERSION" > "$VERSION_STAMP"
