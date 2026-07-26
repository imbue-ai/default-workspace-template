#!/usr/bin/env bash
# Render the workspace's snapshot-pinned apt sources.
#
# Every apt operation in a default-workspace-template environment resolves
# against the Debian archive frozen at a single timestamp T (the committed
# value in .mngr/apt-snapshot-timestamp), so package versions are a pure
# function of T: image builds, boot-time convergence, and agent-initiated
# installs all see the same universe, and versions change only when T is
# deliberately advanced (see the update-self flow).
#
# Source selection:
#   - Default: imbue's mirror at https://apt.imbuepackages.com (a Cloudflare
#     Worker in front of R2; see apps/apt_mirror in the mngr monorepo). It
#     serves indexes frozen verbatim (upstream Debian signatures intact) and
#     read-through-caches the pool, so it is fast and unthrottled. Override
#     the base with APT_MIRROR_BASE_URL.
#   - APT_MIRROR_BASE_URL set but EMPTY: fall back to snapshot.debian.org at
#     the same T directly. Identical content, but snapshot.debian.org
#     throttles, so this is the degraded path for mirror outages.
# Exactly one source set is written: apt update hard-fails when ANY configured
# source is unreachable, so listing both would turn a mirror outage into a
# broken workspace instead of a slow one. Re-run this script to switch.
#
# Check-Valid-Until is disabled because frozen Release files eventually pass
# their Valid-Until date; authenticity still comes from the archive signature.
#
# Usage: write_apt_sources.sh [T]
#   T defaults to the committed .mngr/apt-snapshot-timestamp next to this repo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

SNAPSHOT_TIMESTAMP="${1:-}"
if [ -z "$SNAPSHOT_TIMESTAMP" ]; then
    SNAPSHOT_TIMESTAMP="$(tr -d '[:space:]' < "$REPO_ROOT/.mngr/apt-snapshot-timestamp")"
fi
case "$SNAPSHOT_TIMESTAMP" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
    *) echo "write_apt_sources: invalid snapshot timestamp '$SNAPSHOT_TIMESTAMP' (expected YYYYMMDDTHHMMSSZ)" >&2; exit 1 ;;
esac

# Default only when unset: an explicitly empty APT_MIRROR_BASE_URL is the
# operator's way to force the snapshot.debian.org fallback.
APT_MIRROR_BASE_URL="${APT_MIRROR_BASE_URL-https://apt.imbuepackages.com}"

if [ -n "${APT_MIRROR_BASE_URL:-}" ]; then
    DEBIAN_URI="${APT_MIRROR_BASE_URL%/}/snap/${SNAPSHOT_TIMESTAMP}/debian"
    SECURITY_URI="${APT_MIRROR_BASE_URL%/}/snap/${SNAPSHOT_TIMESTAMP}/debian-security"
else
    DEBIAN_URI="https://snapshot.debian.org/archive/debian/${SNAPSHOT_TIMESTAMP}"
    SECURITY_URI="https://snapshot.debian.org/archive/debian-security/${SNAPSHOT_TIMESTAMP}"
fi

KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg
if [ ! -f "$KEYRING" ]; then
    # The slim python base images carry the archive keys in etc trusted.gpg.d
    # instead of the debian-archive-keyring package layout.
    KEYRING=""
fi

mkdir -p /etc/apt/sources.list.d
# Retire the base image's live-archive sources: exactly one source set may be
# active, and it must be the pinned one.
rm -f /etc/apt/sources.list.d/debian.sources
: > /etc/apt/sources.list

{
    printf 'Types: deb\n'
    printf 'URIs: %s\n' "$DEBIAN_URI"
    printf 'Suites: trixie trixie-updates\n'
    printf 'Components: main\n'
    printf 'Check-Valid-Until: no\n'
    if [ -n "$KEYRING" ]; then printf 'Signed-By: %s\n' "$KEYRING"; fi
    printf '\n'
    printf 'Types: deb\n'
    printf 'URIs: %s\n' "$SECURITY_URI"
    printf 'Suites: trixie-security\n'
    printf 'Components: main\n'
    printf 'Check-Valid-Until: no\n'
    if [ -n "$KEYRING" ]; then printf 'Signed-By: %s\n' "$KEYRING"; fi
} > /etc/apt/sources.list.d/pinned.sources

echo "write_apt_sources: pinned apt sources to ${SNAPSHOT_TIMESTAMP} (${DEBIAN_URI})"
