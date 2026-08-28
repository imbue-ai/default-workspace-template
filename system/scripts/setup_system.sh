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
: "${UV_VERSION:=0.11.7}"
: "${NODE_VERSION:=22.23.2}"
: "${CLAUDE_CODE_VERSION:=2.1.227}"
: "${CODEX_VERSION:=0.147.0}"
: "${PI_VERSION:=0.83.0}"
: "${PLAYWRIGHT_CLI_VERSION:=0.1.18}"
: "${OPENCODE_VERSION:=1.18.19}"
: "${MODAL_VERSION:=1.4.2}"
: "${GH_VERSION:=2.96.0}"
: "${CADDY_VERSION:=2.11.4}"
: "${FRP_VERSION:=0.70.1}"
: "${LATCHKEY_VERSION:=3.4.1}"
: "${RESTIC_VERSION:=0.18.1}"

# Shared curl flags for the pinned-binary downloads below. --retry-all-errors
# also retries a mid-transfer drop (curl exit 56), which plain --retry does not
# cover.
CURL_RETRY=(--retry 5 --retry-all-errors --retry-delay 2)

# Install a downloaded binary atomically: fetch to a temp file beside the target,
# then rename(2) it into place. A plain `curl -o <dest>` truncates <dest> in
# place, which fails with ETXTBSY when <dest> is a currently-running executable --
# e.g. re-provisioning a live workspace whose `terminal` service is running ttyd
# (this is what the update-self reveal flow does, and `set -e` then aborts the
# whole script). rename(2) over a busy
# executable is allowed: running processes keep the old inode while new execs pick
# up the replacement, so download-then-mv is safe to re-run on a live host. The
# temp file shares <dest>'s directory so the mv is a same-filesystem atomic rename,
# and the explicit 0755 reproduces the old `curl -o` (0644) + `chmod +x` result.
install_downloaded_binary() {
    _url="$1"
    _dest="$2"
    _tmp="$(mktemp "${_dest}.XXXXXX")"
    curl -fsSL "${CURL_RETRY[@]}" "$_url" -o "$_tmp"
    chmod 0755 "$_tmp"
    mv -f "$_tmp" "$_dest"
}

# System packages (tini for signal handling; supervisor runs our background
# services; cron runs the recurring jobs, driven from supervisord rather than an
# init system; earlyoom is the OOM-prevention daemon that sheds memory under
# pressure before the kernel kills an arbitrary victim; the rest are
# agent/runtime deps). supervisor provides the system supervisord + supervisorctl
# that `uv run bootstrap` execs into the foreground.
# xvfb + xclip (the browser fleet's virtual display and its clipboard bridge)
# are baked here, NOT deferred to the env.d browser unit: [program:xvfb] execs
# Xvfb directly at boot, and a binary that static service config promises at
# every boot must exist in the image. They are a few MB; only the heavy
# Fortress/Chromium stack stays deferred.
apt-get update
apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates cron curl earlyoom fd-find git git-lfs jq less nano \
    openssh-server procps restic ripgrep rsync sqlite3 supervisor tini tmux unison util-linux wget \
    xclip xvfb xxd xmlstarlet
# Runtime libraries the pixelflux/pcmflux wheels (the browser fleet's H.264 + Opus
# media pipes) dlopen at import -- without libva pixelflux's import raises (guarded in
# videopipe.py) -- plus xdpyinfo, which videopipe uses to size the capture. Baked small.
apt-get install -y --no-install-recommends \
    libva2 libva-drm2 libva-x11-2 libpixman-1-0 x11-utils pulseaudio pulseaudio-utils
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
curl -fsSL "${CURL_RETRY[@]}" "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${restic_goarch}.bz2" -o /tmp/restic.bz2
echo "${restic_sha256}  /tmp/restic.bz2" | sha256sum -c -
bunzip2 -c /tmp/restic.bz2 > /usr/local/bin/restic
chmod +x /usr/local/bin/restic
rm /tmp/restic.bz2

# ttyd (terminal-over-web) binary from GitHub releases (not in apt).
ttyd_arch="$(uname -m)"
install_downloaded_binary "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ttyd_arch}" /usr/local/bin/ttyd

# GitHub CLI as a pinned, sha256-verified GitHub-release tarball. gh is not in
# Debian, and a third-party apt repo would escape the snapshot-pinned mirror,
# so it installs like ttyd: fixed version, checksummed download.
gh_arch="$(uname -m)"
case "${gh_arch}" in
    x86_64) gh_goarch="amd64"; gh_sha256="83d5c2ccad5498f58bf6368acb1ab32588cf43ab3a4b1c301bf36328b1c8bd60" ;;
    aarch64) gh_goarch="arm64"; gh_sha256="06f86ec7103d41993b76cd78072f43595c34aaa56506d971d9860e67140bf909" ;;
    *) echo "Unsupported architecture for gh: ${gh_arch}" >&2; exit 1 ;;
esac
curl -fsSL "${CURL_RETRY[@]}" "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh_goarch}.tar.gz" -o /tmp/gh.tar.gz
echo "${gh_sha256}  /tmp/gh.tar.gz" | sha256sum -c -
tar -xzf /tmp/gh.tar.gz -C /tmp "gh_${GH_VERSION}_linux_${gh_goarch}/bin/gh"
mv -f "/tmp/gh_${GH_VERSION}_linux_${gh_goarch}/bin/gh" /usr/local/bin/gh
chmod 0755 /usr/local/bin/gh
rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${gh_goarch}"

# caddy + frpc for the self-hosted sharing stack (the share-gateway service):
# caddy terminates a shared workspace's TLS in-container; frpc is the outbound
# tunnel to the region's relay. Neither is in Debian's snapshot mirror, so both
# install like gh: fixed version, checksummed GitHub-release tarball.
caddy_arch="$(uname -m)"
case "${caddy_arch}" in
    x86_64) caddy_goarch="amd64"; caddy_sha256="527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9" ;;
    aarch64) caddy_goarch="arm64"; caddy_sha256="52d42ae12b3462097e9868da6dfed3c9648ae12edd3b3638102312af84cb6904" ;;
    *) echo "Unsupported architecture for caddy: ${caddy_arch}" >&2; exit 1 ;;
esac
curl -fsSL "${CURL_RETRY[@]}" "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${caddy_goarch}.tar.gz" -o /tmp/caddy.tar.gz
echo "${caddy_sha256}  /tmp/caddy.tar.gz" | sha256sum -c -
tar -xzf /tmp/caddy.tar.gz -C /tmp caddy
mv -f /tmp/caddy /usr/local/bin/caddy
chmod 0755 /usr/local/bin/caddy
rm -f /tmp/caddy.tar.gz

frp_arch="$(uname -m)"
case "${frp_arch}" in
    x86_64) frp_goarch="amd64"; frp_sha256="333da23d1b9009d7c01638e9ba38cf4600f7d37d393f854e96ee1396adefa9a6" ;;
    aarch64) frp_goarch="arm64"; frp_sha256="3990f396a9a490ee7f0e5f355287750ed41520064ed999eab443b5e9a78d773d" ;;
    *) echo "Unsupported architecture for frp: ${frp_arch}" >&2; exit 1 ;;
esac
curl -fsSL "${CURL_RETRY[@]}" "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${frp_goarch}.tar.gz" -o /tmp/frp.tar.gz
echo "${frp_sha256}  /tmp/frp.tar.gz" | sha256sum -c -
tar -xzf /tmp/frp.tar.gz -C /tmp "frp_${FRP_VERSION}_linux_${frp_goarch}/frpc"
mv -f "/tmp/frp_${FRP_VERSION}_linux_${frp_goarch}/frpc" /usr/local/bin/frpc
chmod 0755 /usr/local/bin/frpc
rm -rf /tmp/frp.tar.gz "/tmp/frp_${FRP_VERSION}_linux_${frp_goarch}"

# uv (pinned). Installs to /root/.local/bin.
curl -LsSf "${CURL_RETRY[@]}" "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
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
curl -fsSL "${CURL_RETRY[@]}" https://claude.ai/install.sh > /tmp/install_claude.sh
bash /tmp/install_claude.sh "${CLAUDE_CODE_VERSION}"
test -x /root/.local/bin/claude
# Fail the build/provision right here on a pin mismatch. mngr's own runtime
# version check still runs when a claude agent is created, but since the
# services agent stopped being a claude agent that check would not fire until
# the first chat agent is created on first boot -- far too late to catch a
# Dockerfile/settings.toml desync cheaply.
installed_claude_version="$(/root/.local/bin/claude --version | awk '{print $1}')"
if [ "${installed_claude_version}" != "${CLAUDE_CODE_VERSION}" ]; then
    echo "Installed claude version ${installed_claude_version} does not match pinned CLAUDE_CODE_VERSION ${CLAUDE_CODE_VERSION}" >&2
    exit 1
fi

# Node.js as a pinned, sha256-verified nodejs.org release tarball (installed to
# /usr/local so node/npm/npx land on PATH). NOT the trixie apt nodejs (20.x): the
# pi CLI ships an `undici` that calls `worker_threads.markAsUncloneable`, which is
# absent on Node 20 and crashes pi at import -- so we pin Node 22 LTS. Installs like
# gh/caddy/restic above: fixed version, checksummed download. Keep NODE_VERSION in
# sync with the Dockerfile ARG.
node_arch="$(uname -m)"
case "${node_arch}" in
    x86_64) node_goarch="x64"; node_sha256="b294a556e639d64338823920e5866c21c02741742d2e1529ee1a225c1ec9252a" ;;
    aarch64) node_goarch="arm64"; node_sha256="013b59cfd2819703a6f4a14ab891fc46fc2a4e3f5bcd92de3fb4929b43e35b30" ;;
    *) echo "Unsupported architecture for node: ${node_arch}" >&2; exit 1 ;;
esac
curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_goarch}.tar.gz" -o /tmp/node.tar.gz
echo "${node_sha256}  /tmp/node.tar.gz" | sha256sum -c -
# --strip-components=1 lands bin/node, bin/npm, bin/npx, lib/node_modules under /usr/local.
tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1
rm /tmp/node.tar.gz
command -v node npm >/dev/null

# Codex CLI (pinned; npm-installed, needs Node.js above). Keep in sync with
# agent_types.codex.version in .mngr/settings.toml. Stock upstream build: the
# custom "codex-in-minds" TUI patch is retired. codex is now driven through its
# own app-server (JSON-RPC) with a visible `codex --remote` TUI, so the fork's
# `/model <model> [effort]` workaround (openai/codex#32212) is no longer needed
# and the vendored binary is left exactly as npm ships it.
npm install -g "@openai/codex@${CODEX_VERSION}"
command -v codex >/dev/null
codex --version

# OpenCode CLI (pinned; standalone binary, no Node needed). Its installer reads
# VERSION and hardcodes $HOME/.opencode/bin, which is NOT on PATH, so symlink the
# binary into /usr/local/bin like the other downloaded tools.
curl -fsSL https://opencode.ai/install | VERSION="${OPENCODE_VERSION}" bash
ln -sf "$HOME/.opencode/bin/opencode" /usr/local/bin/opencode
opencode --version >/dev/null

# Pi CLI (pinned; npm-installed, needs Node 22 above -- crashes on Node 20). Keep
# in sync with agent_types.pi-coding.version in .mngr/settings.toml.
npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}"
command -v pi >/dev/null

npm install -g "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}"
command -v playwright-cli >/dev/null

# Antigravity CLI (agy). Installed via a vendored, version-LOCKED copy of Google's
# installer: the upstream one queries a "latest" manifest, and that manifest is the
# ONLY source of a release's opaque build id, so a version can be pinned only by
# capturing its URL + sha512 while it is current. agy_install-1.1.16.sh holds those.
# Reachable two ways depending on how we were invoked (mirrors the secret-scanner
# dual-name resolution below): in a Dockerfile build it is baked beside this script as
# default-workspace-template-install-agy; run straight from the repo (Lima/Modal) it is
# its sibling agy_install-1.1.16.sh. Installed into /usr/local/bin explicitly rather
# than the installer's $HOME/.local/bin default, so agy sits with every other
# downloaded tool and does not depend on HOME surviving into the runtime image.
agy_installer_dir="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$agy_installer_dir/agy_install-1.1.16.sh" ]; then
    bash "$agy_installer_dir/agy_install-1.1.16.sh" /usr/local/bin
else
    bash "$agy_installer_dir/default-workspace-template-install-agy" /usr/local/bin
fi
# NOT guarded by `command -v agy &&`: errexit ignores a failure on the left of an AND-OR
# list, so a missing binary sailed past and the build verification could not fail. Bare,
# a missing agy is a 127 and `set -e` fires.
agy --version >/dev/null

# Bake the pi extension packages (subagents, web access) into the image at a
# NON-home path: the runtime volume shadows the build-time HOME, so ~/.pi cannot
# be baked directly. `pi install` materialises npm/node_modules plus a
# settings.json package list under PI_CODING_AGENT_DIR; seed_home_skeleton.sh
# copies the npm tree into the real ~/.pi/agent at first boot (a ~1s local copy
# instead of a ~60s networked npm install -- the harness ships with its tools).
# Keep the pins in sync with seed_home_skeleton.sh.
: "${PI_SUBAGENTS_VERSION:=0.45.0}"
: "${PI_WEB_ACCESS_VERSION:=0.19.0}"
PI_CODING_AGENT_DIR=/opt/pi-extensions pi install "npm:pi-subagents@${PI_SUBAGENTS_VERSION}"
PI_CODING_AGENT_DIR=/opt/pi-extensions pi install "npm:pi-web-access@${PI_WEB_ACCESS_VERSION}"
test -d /opt/pi-extensions/npm/node_modules

# apt Post-Invoke capture hook: after EVERY apt/dpkg operation at runtime, the
# environment record under ~/.mngr/plugin/env-converge re-captures from dpkg's
# own database -- zero agent cooperation required ("dpkg is truth"). The hook
# no-ops during image builds and provisioning (no mngr host dir yet) and is
# always best-effort: a capture failure must never break apt itself.
cat > /usr/local/bin/env-converge-capture-hook << 'HOOK'
#!/bin/sh
# Best-effort apt Post-Invoke hook: refresh the environment record.
[ -d /home/user/.mngr ] || exit 0
[ -d /home/user/workspace/system/services/env_converge ] || exit 0
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
# A RUNNING sshd must re-read this drop-in: the listener hands its boot-time
# config to every future session, so without a reload the home move above
# silently breaks all root logins (the stale relative AuthorizedKeysFile
# resolves against the NEW home, which has no authorized_keys). This bites
# lima mode, where this script runs against a live sshd at first boot; in
# docker image builds no sshd is running and this is a no-op.
if command -v systemctl >/dev/null 2>&1 && systemctl is-active ssh >/dev/null 2>&1; then
    systemctl reload ssh
fi

# sshd only reads its configuration at startup, and on providers that start sshd
# before this script runs (Modal: mngr provisions SSH, then runs setup) the
# listener is already up with the stock config. Without a reload it keeps
# resolving AuthorizedKeysFile against the passwd home, which the home move has
# just repointed at /home/user, so every later connection fails to authenticate.
# SIGHUP makes sshd re-exec and re-read the config; established sessions are
# unaffected. No-op when sshd is not running (image builds, Lima provisioning).
#
# This used to work by accident: the workspace's apt phase reinstalled
# openssh-server, which restarted sshd and picked the file up as a side effect.
_sshd_pid="$(pgrep -o -x sshd 2>/dev/null || true)"
if [ -n "$_sshd_pid" ]; then
    kill -HUP "$_sshd_pid"
fi

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

# Secret-scanner binaries (betterleaks + kingfisher) for the publish-template
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
