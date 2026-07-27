#!/usr/bin/env bash
# Shared system-toolchain setup for default-workspace-template hosts.
#
# Installs the repo-independent toolchain: system packages, language runtimes,
# and pinned CLIs. This is the single source of truth for that setup -- the
# Dockerfile RUNs it (docker / vps_docker / ovh providers) and the Lima provider
# runs it directly in the VM as root. It needs no repo content, must run as root,
# and is idempotent so re-running is safe.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Skip if this exact repo tree was already provisioned (e.g. baked into the image).
. "$(dirname "$0")/_provision_guard.sh"
provision_skip_if_done setup_system

# Pin all apt operations to the committed archive snapshot timestamp before the
# first apt-get below. Idempotent: the docker build already ran this (the image
# carries the pinned sources); lima/modal VMs get their sources pinned here.
# Baked into a docker image this script lives at
# /usr/local/bin/default-workspace-template-setup-system beside the RENAMED
# sources script (and the timestamp baked at /etc/...); run straight from the
# repo (Lima/Modal) the sibling write_apt_sources.sh reads the committed
# .mngr/apt-snapshot-timestamp itself. Mirrors the secret-scanner dual-name
# resolution at the bottom of this script.
sources_dir="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$sources_dir/write_apt_sources.sh" ]; then
    bash "$sources_dir/write_apt_sources.sh"
else
    bash "$sources_dir/default-workspace-template-write-apt-sources" \
        "$(cat /etc/default-workspace-template-apt-snapshot-timestamp)"
fi

# Pinned versions (single source of truth; override via env if needed). Keep
# CLAUDE_CODE_VERSION in sync with agent_types.claude.version in .mngr/settings.toml.
: "${TTYD_VERSION:=1.7.7}"
: "${CLOUDFLARED_VERSION:=2026.3.0}"
: "${UV_VERSION:=0.11.7}"
: "${CLAUDE_CODE_VERSION:=2.1.207}"
: "${MODAL_VERSION:=1.4.2}"
: "${GH_VERSION:=2.96.0}"
: "${LATCHKEY_VERSION:=3.1.0}"
: "${RESTIC_VERSION:=0.18.1}"

# Install a downloaded binary atomically: fetch to a temp file beside the target,
# then rename(2) it into place. A plain `curl -o <dest>` truncates <dest> in
# place, which fails with ETXTBSY when <dest> is a currently-running executable --
# e.g. re-provisioning a live workspace whose `terminal` service is running ttyd,
# or whose cloudflared tunnel is active (this is what the update-self reveal flow
# does, and `set -e` then aborts the whole script). rename(2) over a busy
# executable is allowed: running processes keep the old inode while new execs pick
# up the replacement, so download-then-mv is safe to re-run on a live host. The
# temp file shares <dest>'s directory so the mv is a same-filesystem atomic rename,
# and the explicit 0755 reproduces the old `curl -o` (0644) + `chmod +x` result.
install_downloaded_binary() {
    _url="$1"
    _dest="$2"
    _tmp="$(mktemp "${_dest}.XXXXXX")"
    curl -fsSL "$_url" -o "$_tmp"
    chmod 0755 "$_tmp"
    mv -f "$_tmp" "$_dest"
}

# System packages (tini for signal handling; supervisor runs our background
# services; cron runs the recurring jobs, driven from supervisord rather than an
# init system; earlyoom is the OOM-prevention daemon that sheds memory under
# pressure before the kernel kills an arbitrary victim; the rest are
# agent/runtime deps). supervisor provides the system supervisord + supervisorctl
# that `uv run bootstrap` execs into the foreground.
apt-get update
apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates cron curl earlyoom fd-find git git-lfs jq less nano \
    openssh-server procps restic ripgrep rsync sqlite3 supervisor tini tmux unison util-linux wget \
    xxd xmlstarlet
rm -rf /var/lib/apt/lists/*

# The Debian `supervisor` package enables a systemd unit that immediately starts
# a supervisord against the default /etc/supervisor/supervisord.conf. On
# systemd-based providers (lima/VPS) that daemon grabs /var/run/supervisor.sock
# and makes `uv run bootstrap`'s `supervisord -c /home/user/workspace/system/supervisord.conf`
# fail with "Another program is already listening". We always launch our own
# supervisord from bootstrap, so disable + mask the packaged unit. Guarded so
# it is a no-op on docker (no systemd / no systemctl on the slim image).
if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now supervisor 2>/dev/null || true
    systemctl mask supervisor 2>/dev/null || true
    # Same story for cron: our supervisord runs it ([program:cron]), so the
    # packaged systemd unit would double-run every job on systemd hosts.
    systemctl disable --now cron.service 2>/dev/null || true
    systemctl mask cron.service 2>/dev/null || true
fi

# Point supervisor's default config search path at the workspace config so a
# bare `supervisorctl` works from any cwd (the config lives under system/, so
# the old "run it from the repo root" $CWD/supervisord.conf lookup no longer
# applies). Dangles harmlessly until the workspace is seeded at first boot.
ln -sfn /home/user/workspace/system/supervisord.conf /etc/supervisord.conf

# The distro restic (bookworm ships 0.14) predates `restic restore --delete`,
# which the minds in-place backup restore requires (restic >= 0.17). Install
# the pinned release (sha256-verified, from the official SHA256SUMS) at
# /usr/local/bin so it shadows the apt binary and the whole workspace --
# including the hourly host-backup service -- runs the same pinned version
# minds bundles on the desktop side. The apt package above stays as a
# fallback for anything resolving /usr/bin/restic explicitly.
restic_arch="$(uname -m)"
case "${restic_arch}" in
    x86_64) restic_goarch="amd64"; restic_sha256="680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d" ;;
    aarch64) restic_goarch="arm64"; restic_sha256="87f53fddde38764095e9c058a3b31834052c37e5826d2acf34e18923c006bd45" ;;
    *) echo "Unsupported architecture for restic: ${restic_arch}" >&2; exit 1 ;;
esac
curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${restic_goarch}.bz2" -o /tmp/restic.bz2
echo "${restic_sha256}  /tmp/restic.bz2" | sha256sum -c -
bunzip2 -c /tmp/restic.bz2 > /usr/local/bin/restic
chmod +x /usr/local/bin/restic
rm /tmp/restic.bz2

# ttyd (terminal-over-web) binary from GitHub releases (not in apt).
ttyd_arch="$(uname -m)"
install_downloaded_binary "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ttyd_arch}" /usr/local/bin/ttyd

# cloudflared for Cloudflare tunnel support.
cloudflared_arch="$(dpkg --print-architecture)"
install_downloaded_binary "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${cloudflared_arch}" /usr/local/bin/cloudflared

# GitHub CLI as a pinned, sha256-verified GitHub-release tarball. gh is not in
# Debian, and a third-party apt repo would escape the snapshot-pinned mirror,
# so it installs like ttyd/cloudflared: fixed version, checksummed download.
gh_arch="$(uname -m)"
case "${gh_arch}" in
    x86_64) gh_goarch="amd64"; gh_sha256="83d5c2ccad5498f58bf6368acb1ab32588cf43ab3a4b1c301bf36328b1c8bd60" ;;
    aarch64) gh_goarch="arm64"; gh_sha256="06f86ec7103d41993b76cd78072f43595c34aaa56506d971d9860e67140bf909" ;;
    *) echo "Unsupported architecture for gh: ${gh_arch}" >&2; exit 1 ;;
esac
curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh_goarch}.tar.gz" -o /tmp/gh.tar.gz
echo "${gh_sha256}  /tmp/gh.tar.gz" | sha256sum -c -
tar -xzf /tmp/gh.tar.gz -C /tmp "gh_${GH_VERSION}_linux_${gh_goarch}/bin/gh"
mv -f "/tmp/gh_${GH_VERSION}_linux_${gh_goarch}/bin/gh" /usr/local/bin/gh
chmod 0755 /usr/local/bin/gh
rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${gh_goarch}"

# uv (pinned). Installs to /root/.local/bin.
curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
export PATH="/root/.local/bin:$PATH"

# Ensure a uv-managed Python that satisfies the workspace lockfile (>=3.12).
# The Docker base image ships 3.12, but other bases (e.g. a Debian VM whose
# system Python is 3.11) do not -- and the root pyproject's requires-python
# (>=3.11) lets uv otherwise pick the system 3.11, which the frozen lock then
# rejects. Fetch a managed 3.12 here so install_dependencies.sh /
# build_workspace.sh can pin uv to it. No-op when system Python is already
# >=3.12, so the Docker build is unchanged.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    uv python install 3.12
fi

# Make /root/.local/bin discoverable in login and interactive shells. The docker
# image also sets ENV PATH; the Lima VM relies on these profile writes.
if ! grep -q '/root/.local/bin' /root/.bashrc 2>/dev/null; then
    echo 'PATH="/root/.local/bin:$PATH"' >> /root/.bashrc
fi
printf '%s\n' 'PATH="/root/.local/bin:$PATH"' > /etc/profile.d/default_workspace_template_path.sh

# Source /home/user/.mngr/env (when present) for interactive bash sessions so terminals can
# run mngr commands without manual setup.
if ! grep -q '/home/user/.mngr/env' /root/.bashrc 2>/dev/null; then
    printf '%s\n' 'if [ -f /home/user/.mngr/env ]; then set -a; . /home/user/.mngr/env; set +a; fi' >> /root/.bashrc
fi

# Claude Code CLI (pinned; the provisioning-time version check expects this exact version).
curl -fsSL https://claude.ai/install.sh > /tmp/install_claude.sh
bash /tmp/install_claude.sh "${CLAUDE_CODE_VERSION}"
test -x /root/.local/bin/claude

# Node.js from trixie main (pinned by the snapshot timestamp like every other
# apt package; trixie ships the nodejs 20.x line). npm is its own package on
# Debian, unlike the NodeSource builds that bundled it.
apt-get update
apt-get install -y --no-install-recommends nodejs npm
rm -rf /var/lib/apt/lists/*

# apt Post-Invoke capture hook: after EVERY apt/dpkg operation at runtime, the
# environment record under ~/.mngr/plugin/env-converge re-captures from dpkg's
# own database -- zero agent cooperation required ("dpkg is truth"). The hook
# no-ops during image builds and provisioning (no mngr host dir yet) and is
# always best-effort: a capture failure must never break apt itself.
cat > /usr/local/bin/env-converge-capture-hook << 'HOOK'
#!/bin/sh
# Best-effort apt Post-Invoke hook: refresh the environment record.
[ -d /home/user/.mngr ] || exit 0
[ -d /home/user/workspace/system/libs/env_converge ] || exit 0
cd /home/user/workspace || exit 0
MNGR_HOST_DIR="${MNGR_HOST_DIR:-/home/user/.mngr}" timeout 120 uv run env-converge capture >/dev/null 2>&1 || true
HOOK
chmod +x /usr/local/bin/env-converge-capture-hook
printf 'DPkg::Post-Invoke { "/usr/local/bin/env-converge-capture-hook || true"; };\n' \
    > /etc/apt/apt.conf.d/90env-converge-capture

# Root's passwd home moves to /home/user (the persistent volume) at the end of
# the image build / VM provisioning, but mngr's SSH provisioning writes root's
# authorized_keys to /root/.ssh -- tooling-owned and container-local, exactly
# where it should live (never backed up, never clobbered by a restore). Point
# sshd at BOTH the passwd-home default and /root/.ssh so that provisioning
# keeps working across the home move. Debian's sshd_config includes
# /etc/ssh/sshd_config.d/*.conf by default.
mkdir -p /etc/ssh/sshd_config.d
printf 'AuthorizedKeysFile .ssh/authorized_keys /root/.ssh/authorized_keys\n' \
    > /etc/ssh/sshd_config.d/60-workspace-root-keys.conf

# Pre-seed github.com SSH host keys so git operations don't block on interactive
# host-key confirmation. Idempotent: only added when absent.
mkdir -p /root/.ssh
chmod 700 /root/.ssh
if ! grep -q "github.com" /root/.ssh/known_hosts 2>/dev/null; then
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> /root/.ssh/known_hosts
fi
chmod 600 /root/.ssh/known_hosts

# latchkey (gateway CLI) and modal (python tool).
npm install -g "latchkey@${LATCHKEY_VERSION}"
uv tool install "modal==${MODAL_VERSION}"

# Secret-scanner binaries (betterleaks + kingfisher) for the publish-inspiration
# scan gate. install_secret_scanners.sh is the single source of truth for the
# version pins + per-arch sha256s; invoking it here means BOTH docker-built
# images (this script runs in a Dockerfile RUN) and Lima-provisioned VMs (this
# script runs directly in the VM) bake in the scanners from one common place.
# The installer is reachable two ways depending on how we were invoked: in a
# Dockerfile build it sits beside this script's install path as
# default-workspace-template-install-secret-scanners; run straight from the repo (Lima/Modal)
# it is its sibling install_secret_scanners.sh. It is idempotent (skips any tool
# already at its pinned version without network access).
setup_dir="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$setup_dir/install_secret_scanners.sh" ]; then
    bash "$setup_dir/install_secret_scanners.sh"
else
    bash "$setup_dir/default-workspace-template-install-secret-scanners"
fi

# Playwright + Chromium is deliberately NOT installed here; the deferred-install
# service installs it idempotently on first boot.

provision_mark_done setup_system
