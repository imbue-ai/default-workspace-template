#!/bin/bash
# Install the pinned Grok Build CLI (`grok`) from xAI, checked against the
# per-arch sha256 recorded here (xAI publishes none). A grok already at
# GROK_VERSION is left in place.
#
# Keep GROK_VERSION at the version mngr's grok plugin is tested with. To bump:
# download https://x.ai/cli/grok-<version>-linux-{x86_64,aarch64}.gz, then set
# GROK_VERSION and both sha256s below from the downloaded files.
set -euo pipefail

GROK_VERSION="1.0.62"
: "${GROK_INSTALL_PATH:=/usr/local/bin/grok}"

# `grok --version` prints `grok <version> (<build>)`.
reports_pinned_version() {
    case "$("$1" --version 2>/dev/null || true)" in
        "grok ${GROK_VERSION} ("*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -x "$GROK_INSTALL_PATH" ] && reports_pinned_version "$GROK_INSTALL_PATH"; then
    exit 0
fi

arch="$(uname -m)"
case "$arch" in
    x86_64 | amd64)
        asset_arch="x86_64"
        sha256="28fd2f6a11ad55bbf760441a2968ca046a53f2396c9029dec81c02f1574657ba"
        ;;
    aarch64 | arm64)
        asset_arch="aarch64"
        sha256="7a8380cf7cc70b7ced5a3ed81311ff22234ad940e02f4c3429b835bbee2a93ad"
        ;;
    *) echo "install_grok: unsupported arch $arch" >&2; exit 1 ;;
esac

asset="grok-${GROK_VERSION}-linux-${asset_arch}.gz"
tmp="$(mktemp -d)"
staged=""
trap 'rm -rf "$tmp" ${staged:+"$staged"}' EXIT
staged="$(mktemp "${GROK_INSTALL_PATH}.XXXXXX")"

curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors -o "${tmp}/${asset}" "https://x.ai/cli/${asset}"
if ! echo "${sha256}  ${tmp}/${asset}" | sha256sum -c --status -; then
    echo "install_grok: ${asset} does not match its pinned sha256 ${sha256}" >&2
    exit 1
fi

# Staged beside the target, so it runs where /tmp is noexec and the rename is atomic.
gzip -dc "${tmp}/${asset}" > "$staged"
chmod 0755 "$staged"
if ! reports_pinned_version "$staged"; then
    echo "install_grok: the ${asset} binary does not report grok ${GROK_VERSION}" >&2
    exit 1
fi
mv -f "$staged" "$GROK_INSTALL_PATH"
