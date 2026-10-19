#!/usr/bin/env bash
# env.d unit: the Screen's stack (Xorg with the dummy driver, XFCE, PulseAudio,
# GStreamer with x264, xdotool, wmctrl, xclip) and the pinned neko v3 server
# binary, plus Fortress (stealth Chromium, the Screen's browser) with its apt
# system libs and the two Chrome extensions, and the screen-browser-open command
# (the x-www-browser alternative). Too heavy to bake into the Docker
# image, and no boot-time service execs any of it directly (the Screen service
# answers "installing" until this unit is satisfied, and its browser launch path
# "Chromium is still installing" until Fortress arrives), so it installs on
# first boot via the `env-converge` one-shot.
#
# env.d contract: idempotent with a fast satisfied-check -- NO marker files.
# The converger re-runs every unit on every boot; a satisfied unit exits 0 in
# milliseconds, and version stability comes from the pins in this script (a
# re-run never silently changes versions -- only a pin bump, landed via
# update-self, does). apt installs come from the snapshot mirror
# .mngr/apt-snapshot-timestamp pins (system/scripts/write_apt_sources.sh).
set -euo pipefail

readonly REPO_ROOT="${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}"

_log() {
    printf '[env.d/screen] %s\n' "$*"
}

# Environments that never use the Screen (e.g. the Imbue Studio CI snapshot
# producer) opt out of the whole unit -- most importantly the hundreds-of-MB
# Fortress and XFCE downloads -- by setting DWT_SKIP_BROWSER_UNIT=1 in the
# agent environment. An env var rather than a marker file keeps the env.d
# contract: the decision is re-evaluated every
# boot, so a workspace whose environment stops setting it simply converges the
# stack on its next boot.
if [ "${DWT_SKIP_BROWSER_UNIT:-}" = "1" ]; then
    _log "DWT_SKIP_BROWSER_UNIT=1 -- skipping the Screen stack install"
    exit 0
fi

_recover_interrupted_dpkg() {
    # A prior apt/dpkg run killed mid-operation leaves dpkg broken, after which
    # every `apt-get install` aborts. This happens routinely for pool hosts: the
    # bake's `mngr stop` parks the host while this deferred install's first-boot
    # `apt` is still running, so the post-lease retry would otherwise fail
    # forever. Recover up front -- each step is a fast no-op when dpkg is already
    # consistent. Best-effort: we log failures loudly (not silently) and let the
    # apt step below surface the real error.
    #
    # Two distinct breakages need two different repairs:
    #   1. Killed during *configure*: packages are unpacked-but-unconfigured;
    #      `dpkg --configure -a` finishes them.
    #   2. Killed during *unpack*: the half-unpacked package gets dpkg's
    #      reinst-required ("R") flag (the "very bad inconsistent state" error),
    #      which `dpkg --configure -a` CANNOT fix -- it skips reinst-required
    #      packages. Those must be reinstalled.
    if ! dpkg --configure -a; then
        _log "WARNING: 'dpkg --configure -a' returned non-zero; continuing with broken-package repair"
    fi
    # Reinstall any package left reinst-required (the 3rd char of dpkg's status
    # abbreviation is "R"); only a reinstall repairs a half-unpacked package.
    local reinst_required
    reinst_required="$(dpkg-query -W -f '${Package} ${db:Status-Abbrev}\n' 2>/dev/null \
        | awk 'substr($2, 3, 1) == "R" { print $1 }')"
    if [ -n "$reinst_required" ]; then
        # shellcheck disable=SC2086
        _log "reinstalling packages left reinst-required by an interrupted unpack: $(echo $reinst_required | tr '\n' ' ')"
        # shellcheck disable=SC2086
        if ! apt-get install --reinstall -y $reinst_required; then
            _log "WARNING: reinstall of reinst-required packages returned non-zero; the apt install below may still fail"
        fi
    fi
    # Finally, let apt repair any remaining broken dependencies (no-op when clean).
    if ! apt-get --fix-broken install -y; then
        _log "WARNING: 'apt-get --fix-broken install' returned non-zero; the apt install below may still fail"
    fi
}

# The apt lists are removed from the image after the bake, so an install needs
# one refresh of the pinned snapshot's indexes first; once per unit run.
_apt_lists_refreshed=0
_apt_install() {
    if [ "$_apt_lists_refreshed" -eq 0 ]; then
        if ! apt-get update -y; then
            _log "apt-get update FAILED; the next converge retries"
            return 1
        fi
        _apt_lists_refreshed=1
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

# The Screen's packages: Xorg
# with the dummy driver (xserver-xorg-core carries cvt), XFCE as the plain
# metapackage without the goodies plus its terminal, PulseAudio, GStreamer with
# the x264 encoder (in plugins-ugly), the X and PulseAudio capture elements,
# the tools the service drives the display with (xdotool, wmctrl, xclip,
# xrandr from x11-xserver-utils), dbus-x11 for the session bus startxfce4
# launches, x11-apps for xeyes (the smoke test's window), and the shared
# libraries the neko binary links that nothing else here pulls in.
readonly _SCREEN_APT_PACKAGES=(
    xserver-xorg-core
    xserver-xorg-video-dummy
    xfce4
    xfce4-terminal
    pulseaudio
    pulseaudio-utils
    gstreamer1.0-plugins-base
    gstreamer1.0-plugins-good
    gstreamer1.0-plugins-bad
    gstreamer1.0-plugins-ugly
    gstreamer1.0-x
    gstreamer1.0-pulseaudio
    xdotool
    wmctrl
    xclip
    x11-xserver-utils
    x11-apps
    dbus-x11
    libgstreamer-plugins-base1.0-0
    libgtk-3-0t64
    libxcvt0
    libxtst6
    libxres1
)

_install_screen_packages() {
    # Fast satisfied-check (env.d contract: no marker files): the binaries the
    # Screen's programs exec and the x264 encoder plugin are in place.
    if command -v Xorg >/dev/null 2>&1 \
        && command -v startxfce4 >/dev/null 2>&1 \
        && command -v xfce4-terminal >/dev/null 2>&1 \
        && command -v pulseaudio >/dev/null 2>&1 \
        && command -v xdotool >/dev/null 2>&1 \
        && command -v wmctrl >/dev/null 2>&1 \
        && command -v xclip >/dev/null 2>&1 \
        && command -v xrandr >/dev/null 2>&1 \
        && command -v xeyes >/dev/null 2>&1 \
        && command -v dbus-launch >/dev/null 2>&1 \
        && [ -n "$(find /usr/lib -path '*/gstreamer-1.0/libgstx264.so' -print -quit 2>/dev/null)" ] \
        && [ -n "$(find /usr/lib -path '*/gstreamer-1.0/libgstximagesrc.so' -print -quit 2>/dev/null)" ] \
        && [ -n "$(find /usr/lib -path '*/gstreamer-1.0/libgstpulseaudio.so' -print -quit 2>/dev/null)" ] \
        && [ -n "$(find /usr/lib -path '*/xorg/modules/drivers/dummy_drv.so' -print -quit 2>/dev/null)" ]; then
        _log "screen packages: already installed, satisfied"
        return 0
    fi
    _recover_interrupted_dpkg
    _log "screen packages: installing ${#_SCREEN_APT_PACKAGES[@]} apt packages"
    if ! _apt_install "${_SCREEN_APT_PACKAGES[@]}"; then
        _log "screen packages: apt install FAILED; the next converge retries"
        return 1
    fi
    _log "screen packages: install complete"
}

# The neko v3 server binary and its Xorg input driver (m1k1o/neko, Apache-2.0). neko
# publishes no release binary (its GitHub releases carry no assets), so the two files
# this needs, /usr/bin/neko and the touchscreen module /usr/lib/xorg/modules/input/
# neko_drv.so, are taken from the pinned ghcr.io/m1k1o/neko/base image through the OCI
# registry API: an anonymous pull token, then the one layer blob that the image's COPY
# step for each file produced, pinned by its digest per architecture, extracted with
# tar, and checked against the pinned SHA-256 of the file itself. The module goes into
# the server's input module directory, where Xorg finds the ``neko`` driver the Screen's
# xorg.conf names; the image builds it on Debian trixie against the same xorg-server
# (21.1, XInput ABI 24.4) the pinned snapshot installs here.
#
# How the pins were computed (2026-10-17, tag 3.2.0 = neko v3.2.0):
#   TOKEN=$(curl -fsSL 'https://ghcr.io/token?scope=repository:m1k1o/neko/base:pull' | jq -r .token)
#   curl -fsSL -H "Authorization: Bearer $TOKEN" \
#        -H 'Accept: application/vnd.oci.image.index.v1+json' \
#        https://ghcr.io/v2/m1k1o/neko/base/manifests/3.2.0
#     -> the index (docker-content-digest sha256:587a3389b6b99c8999eb00a768c79205a74647c57cf5b9695a90234a0edaa446)
#        names one manifest per platform: linux/amd64 sha256:37fb9f9f6a34608f7d25fa0ed0ecccb6c4ef9563dc2feaf8cc493cb43939fce5,
#        linux/arm64 sha256:3629b012656d3d2a76bc9b21c4ea1ebd03f4f1366383490d6065df0e332619e8.
#   For each: fetch the manifest and its config blob, pair the manifest's layers
#   with the config's non-empty history entries in order; the layer whose
#   created_by reads "COPY /src/bin/neko /usr/bin/neko" holds the binary and the one
#   reading "COPY /usr/local/lib/xorg/modules/input/neko_drv.so ..." the module.
#   Download each layer blob, `tar -xzf -` the member, `sha256sum` the result.
readonly _NEKO_IMAGE_REPOSITORY="m1k1o/neko/base"
readonly _NEKO_IMAGE_TAG="3.2.0"
readonly _NEKO_AMD64_LAYER_DIGEST="sha256:bde5ab1e10a03c81b0c0fc4a031d1a9c50eb297e212f3516f99adb02c460f18c"
readonly _NEKO_AMD64_SHA256="265c202f1b99d9e85aa3fe2a3c38de6ce4f3c698aa16c0b0250ebf1f7f4b98ca"
readonly _NEKO_ARM64_LAYER_DIGEST="sha256:cfe02384b17eaba05759c333efebc2b594de1de2eb6e6e28f5d2687ee58c5764"
readonly _NEKO_ARM64_SHA256="39fab8e67365509244d24ea9378d72d88781f6ade9914f953be7051ec91fe9d7"
readonly _NEKO_INSTALL_PATH="/opt/neko/bin/neko"
readonly _NEKO_INPUT_AMD64_LAYER_DIGEST="sha256:ab1fea03845aff1fdc09bf155f11a43bfecea8185c43f922a4b63ae6dc2e6d2b"
readonly _NEKO_INPUT_AMD64_SHA256="548b696c876b93deea6a7a7bc75e02ad8731eba9e0d89a08873e7a8c59ebc514"
readonly _NEKO_INPUT_ARM64_LAYER_DIGEST="sha256:b6e0e2bca85ce2c55e15fe0a8da457ce61aa2551398c03e2efef321554c20cff"
readonly _NEKO_INPUT_ARM64_SHA256="bfb0948d00abc7fe8112d0e4b91d0be19ff4551cee0cbcea6b196ae6135d8999"
readonly _NEKO_INPUT_IMAGE_MEMBER="usr/lib/xorg/modules/input/neko_drv.so"
readonly _NEKO_INPUT_INSTALL_PATH="/usr/lib/xorg/modules/input/neko_drv.so"

_file_sha256() {
    sha256sum "$1" | awk '{print $1}'
}

# An anonymous pull token for the neko base image, on stdout (the logging goes to stderr,
# since callers capture stdout).
_neko_token() {
    local token
    _log "neko: fetching a pull token for ghcr.io/$_NEKO_IMAGE_REPOSITORY" >&2
    token="$(curl -fsSL --max-time 60 "https://ghcr.io/token?scope=repository:${_NEKO_IMAGE_REPOSITORY}:pull" \
        | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
    if [ -z "$token" ]; then
        _log "neko: could not get a registry token; the next converge retries" >&2
        return 1
    fi
    printf '%s' "$token"
}

# Install one file of the image: the member <member> of the layer <layer_digest>, whose
# SHA-256 is <sha256>, at <destination> with mode <mode>. Idempotent: a destination that
# already hashes to the pin is left alone (hashing 27 MB takes tens of milliseconds, and
# it is what makes a pin bump re-install).
_install_neko_image_file() {
    local name="$1" layer_digest="$2" member="$3" sha256="$4" destination="$5" mode="$6"
    if [ -f "$destination" ] && [ "$(_file_sha256 "$destination")" = "$sha256" ]; then
        _log "neko: $name already installed at $destination, satisfied"
        return 0
    fi
    local token
    token="$(_neko_token)" || return 1
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_dir'" RETURN
    local layer="$tmp_dir/layer.tar.gz"
    _log "neko: downloading layer $layer_digest of ${_NEKO_IMAGE_REPOSITORY}:${_NEKO_IMAGE_TAG} for the $name"
    if ! curl -fsSL --retry 3 --max-time 600 -H "Authorization: Bearer $token" \
        -o "$layer" "https://ghcr.io/v2/${_NEKO_IMAGE_REPOSITORY}/blobs/${layer_digest}"; then
        _log "neko: layer download FAILED; the next converge retries"
        return 1
    fi
    if [ "sha256:$(_file_sha256 "$layer")" != "$layer_digest" ]; then
        _log "neko: layer digest mismatch -- refusing to install the $name"
        return 1
    fi
    if ! tar -xzf "$layer" -C "$tmp_dir" "$member"; then
        _log "neko: the layer holds no $member; the pin is wrong"
        return 1
    fi
    if [ "$(_file_sha256 "$tmp_dir/$member")" != "$sha256" ]; then
        _log "neko: $name SHA256 mismatch -- refusing to install"
        return 1
    fi
    if ! install -D -m "$mode" "$tmp_dir/$member" "$destination"; then
        _log "neko: could not install the $name at $destination; the next converge retries"
        return 1
    fi
    _log "neko: $name install complete ($destination)"
}

_install_neko() {
    local layer_digest sha256 input_layer_digest input_sha256
    case "$(uname -m)" in
        x86_64)
            layer_digest="$_NEKO_AMD64_LAYER_DIGEST"; sha256="$_NEKO_AMD64_SHA256"
            input_layer_digest="$_NEKO_INPUT_AMD64_LAYER_DIGEST"; input_sha256="$_NEKO_INPUT_AMD64_SHA256" ;;
        aarch64)
            layer_digest="$_NEKO_ARM64_LAYER_DIGEST"; sha256="$_NEKO_ARM64_SHA256"
            input_layer_digest="$_NEKO_INPUT_ARM64_LAYER_DIGEST"; input_sha256="$_NEKO_INPUT_ARM64_SHA256" ;;
        *) _log "neko: unsupported architecture $(uname -m)"; return 1 ;;
    esac
    local rc=0
    _install_neko_image_file "binary" "$layer_digest" "usr/bin/neko" "$sha256" "$_NEKO_INSTALL_PATH" 0755 || rc=$?
    _install_neko_image_file "input driver" "$input_layer_digest" "$_NEKO_INPUT_IMAGE_MEMBER" "$input_sha256" \
        "$_NEKO_INPUT_INSTALL_PATH" 0644 || rc=$?
    return "$rc"
}

# Fortress (tiliondev/fortress), the stealth Chromium the Screen's browser runs. Pinned
# at v151, the last BSD release: v153 (Fortress v3) moves to a source-available license
# that charges organizations of our size for production use, and its engine-level
# "recorded-human mouse motion" makes a CDP mouse click hang under Playwright (1.58 and
# 1.64 alike, with the raw-engine switch, and with the launcher bypassed), which would
# stall every agent driving the browser fleet. arm64 has no official v151 build
# (tiliondev/fortress#29), so it comes from a fork build.
# The tarballs unpack to tilion-fortress/ (the `tilion` launcher execs `chrome` beside
# it). The tree is installed under a versioned directory and the stable path every
# consumer knows, /opt/fortress/tilion-fortress/tilion, is a symlink repointed at it,
# so a pin bump fails the satisfied-check and installs the new release.
readonly _FORTRESS_RELEASE="v151.0.7908.0"
readonly _FORTRESS_UNPACKED_DIR="tilion-fortress"
readonly _FORTRESS_X64_URL="https://github.com/tiliondev/fortress/releases/download/${_FORTRESS_RELEASE}/tilion-fortress-linux-x64.tar.gz"
readonly _FORTRESS_X64_SHA256="243238b2b8a8b944b7ba2b63533d2b917da7d569dcb290ce96bf28151294b873"
readonly _FORTRESS_ARM64_URL="https://github.com/minhtrinh-imbue/fortress/releases/download/linux-arm64-151.0.7908.0-debian12/tilion-fortress-linux-arm64-debian12.tar.gz"
readonly _FORTRESS_ARM64_SHA256="da6965af8fa8e995d137bcabdca8d163fde7f32ba483eaf5e029223995f19ada"
readonly _FORTRESS_INSTALL_DIR="/opt/fortress"
readonly _FORTRESS_VERSIONED_DIR="${_FORTRESS_INSTALL_DIR}/fortress-${_FORTRESS_RELEASE}"
readonly _FORTRESS_STABLE_LINK="${_FORTRESS_INSTALL_DIR}/tilion-fortress/tilion"

_install_fortress() {
    # Fast satisfied-check: the pinned release's launcher is in place and the
    # stable path resolves to it (its apt system libs were installed in the same
    # pass that produced it).
    if [ -x "$_FORTRESS_VERSIONED_DIR/tilion" ] \
        && [ "$(readlink -f "$_FORTRESS_STABLE_LINK" 2>/dev/null)" = "$_FORTRESS_VERSIONED_DIR/tilion" ]; then
        _log "fortress: $_FORTRESS_RELEASE already installed at $_FORTRESS_VERSIONED_DIR, satisfied"
        return 0
    fi
    # CLEANUP: drop once every image baked before 2026-10-18 is gone; that unit unpacked this
    # same release straight into the stable path rather than under a versioned directory.
    if [ ! -L "$_FORTRESS_STABLE_LINK" ] && [ -x "$_FORTRESS_STABLE_LINK" ] \
        && "$_FORTRESS_INSTALL_DIR/tilion-fortress/chrome" --version 2>/dev/null | grep -q "${_FORTRESS_RELEASE#v}"; then
        _log "fortress: $_FORTRESS_RELEASE already installed at $_FORTRESS_STABLE_LINK by the previous unit, satisfied"
        return 0
    fi
    # `install-deps` apt-installs the shared libs Chromium needs (libnss3,
    # libgbm, etc.) -- Fortress is a Chromium build too, same requirement.
    _recover_interrupted_dpkg
    _log "fortress: installing apt system libs"
    # `python -m playwright` (not the console script) on purpose: console-script
    # shebangs are path-bound to the venv's build location, which breaks when
    # this unit runs against the relocated /docker_build_code tree during the
    # imbue_cloud slice bake. `python -m` resolves through the interpreter
    # symlink and works from either location.
    if ! (cd "$REPO_ROOT" && uv run python -m playwright install-deps chromium); then
        _log "fortress: apt install FAILED; the next converge retries"
        return 1
    fi

    local url sha256
    case "$(uname -m)" in
        x86_64) url="$_FORTRESS_X64_URL"; sha256="$_FORTRESS_X64_SHA256" ;;
        aarch64) url="$_FORTRESS_ARM64_URL"; sha256="$_FORTRESS_ARM64_SHA256" ;;
        *) _log "fortress: unsupported architecture $(uname -m)"; return 1 ;;
    esac
    _log "fortress: downloading $url"
    # Staged on disk beside the install: /tmp is a 1 GiB tmpfs in the workspace, and the
    # tarball plus its 600 MB tree do not fit there.
    mkdir -p "$_FORTRESS_INSTALL_DIR"
    local stage_dir
    stage_dir="$(mktemp -d "${_FORTRESS_INSTALL_DIR}/.install.XXXXXX")"
    # shellcheck disable=SC2064
    trap "rm -rf '$stage_dir'" RETURN
    local asset="$stage_dir/fortress.tar.gz"
    if ! curl -fsSL --retry 3 --max-time 1200 -o "$asset" "$url"; then
        _log "fortress: download FAILED; the next converge retries"
        return 1
    fi
    if [ "$(_file_sha256 "$asset")" != "$sha256" ]; then
        _log "fortress: SHA256 mismatch -- refusing to install"
        return 1
    fi
    if ! tar xzf "$asset" -C "$stage_dir"; then
        _log "fortress: extract FAILED; the next converge retries"
        return 1
    fi
    rm -f "$asset"
    if [ ! -f "$stage_dir/$_FORTRESS_UNPACKED_DIR/tilion" ] || [ ! -f "$stage_dir/$_FORTRESS_UNPACKED_DIR/chrome" ]; then
        _log "fortress: the tarball does not unpack to $_FORTRESS_UNPACKED_DIR/{tilion,chrome}; the pin is wrong"
        return 1
    fi
    # The previous release goes before the new one lands, so the directory never
    # holds two Chromiums; the extensions beside it are kept.
    find "$_FORTRESS_INSTALL_DIR" -mindepth 1 -maxdepth 1 -name 'fortress-v*' -exec rm -rf {} +
    rm -rf "${_FORTRESS_INSTALL_DIR}/tilion-fortress"
    mv "$stage_dir/$_FORTRESS_UNPACKED_DIR" "$_FORTRESS_VERSIONED_DIR"
    chmod +x "$_FORTRESS_VERSIONED_DIR/tilion" "$_FORTRESS_VERSIONED_DIR/chrome"
    mkdir -p "$(dirname "$_FORTRESS_STABLE_LINK")"
    ln -sfn "$_FORTRESS_VERSIONED_DIR/tilion" "$_FORTRESS_STABLE_LINK"
    # Point Playwright's DEFAULT chromium at Fortress too. A bare `chromium.launch()`
    # (no executable_path) looks in Playwright's own browser cache, which this install
    # deliberately leaves empty (we run `install-deps`, not `install`, so no managed
    # Chromium is downloaded). Symlink Fortress into that expected path -- resolved
    # from Playwright itself so it tracks the pinned version's revision/layout -- so
    # ad-hoc Playwright calls use the same one engine instead of erroring on a missing
    # build. Chromium finds its resources via /proc/self/exe (the real Fortress dir),
    # so symlinking just the launcher is enough.
    local pw_chrome
    pw_chrome="$(cd "$REPO_ROOT" && uv run python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()' 2>/dev/null)"
    if [ -n "$pw_chrome" ]; then
        mkdir -p "$(dirname "$pw_chrome")"
        ln -sfn "$_FORTRESS_STABLE_LINK" "$pw_chrome"
        # Some Playwright versions gate launch() on a per-browser install marker.
        touch "$(dirname "$(dirname "$pw_chrome")")/INSTALLATION_COMPLETE" 2>/dev/null || true
        _log "fortress: pointed Playwright's default chromium at Fortress ($pw_chrome)"
    else
        _log "fortress: WARNING could not resolve Playwright's chromium path; a bare chromium.launch() will still need an explicit executable_path"
    fi
    _log "fortress: install complete ($_FORTRESS_RELEASE at $_FORTRESS_STABLE_LINK)"
}

# Unpacked Chrome extensions loaded into the Screen's browser (the Screen service
# passes them with --load-extension, see its chrome_args). uBlock Origin Lite keeps
# ad noise out of the accessibility snapshots the agent reads; "I still don't care
# about cookies" clears the consent walls it would otherwise have to click through.
#
# NOT version-pinned, deliberately. The CRX endpoint only serves the CURRENT build for an
# id, so a "pin" here could only ever be a post-hoc check that logged a mismatch -- pinning
# theatre, not a pin. Freezing these for real would mean vendoring the CRX into the image or
# mirroring it, which is not worth it for two ad/cookie blockers. The fetch happens once at
# converge, not on a user's first browser launch, and the set is chosen here.
readonly _EXTENSIONS_DIR="${_FORTRESS_INSTALL_DIR}/extensions"
readonly _UBLOCK_ID="ddkjiahejlhfcafbddmgiahcphecmpfh"
readonly _COOKIES_ID="edibdbjcniadpccecjdfdjjppcpchdlm"

_install_one_extension() {
    local name="$1" ext_id="$2" dest="${_EXTENSIONS_DIR}/$1"
    if [ -f "$dest/manifest.json" ]; then
        return 0
    fi
    local crx="/tmp/${ext_id}.crx"
    local url="https://clients2.google.com/service/update2/crx?response=redirect&prodversion=151&acceptformat=crx3&x=id%3D${ext_id}%26uc"
    _log "extensions: fetching ${name} (${ext_id})"
    if ! curl -fsSL --retry 3 --max-time 120 -o "$crx" "$url"; then
        _log "extensions: download FAILED for ${name}; the browser runs without it"
        return 1
    fi
    mkdir -p "$dest"
    # A .crx is a zip with a signature header; unzip skips the header with a warning, which
    # is its exit status 1, so only a status past that or a missing manifest is a failure.
    local unzip_status=0
    unzip -qo "$crx" -d "$dest" >/dev/null 2>&1 || unzip_status=$?
    if [ "$unzip_status" -gt 1 ] || [ ! -f "$dest/manifest.json" ]; then
        _log "extensions: unpack FAILED for ${name} (unzip exit ${unzip_status}); the browser runs without it"
        rm -rf "$dest" "$crx"
        return 1
    fi
    rm -f "$crx"
    local got
    got="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$dest/manifest.json" | head -1)"
    _log "extensions: ${name} ${got} installed"
}

_install_extensions() {
    if [ -f "${_EXTENSIONS_DIR}/ublock-origin-lite/manifest.json" ] \
        && [ -f "${_EXTENSIONS_DIR}/i-still-dont-care-about-cookies/manifest.json" ]; then
        _log "extensions: already installed, satisfied"
        return 0
    fi
    command -v unzip >/dev/null 2>&1 || _apt_install unzip || return 1
    mkdir -p "$_EXTENSIONS_DIR"
    local rc=0
    _install_one_extension "ublock-origin-lite" "$_UBLOCK_ID" || rc=$?
    _install_one_extension "i-still-dont-care-about-cookies" "$_COOKIES_ID" || rc=$?
    return "$rc"
}

# The browser command the Screen's XFCE session runs (system/apps/screen/scripts/
# screen-browser-open, the Screen's own browser launch path): on PATH for the
# helper entry the Screen service renders into the home, and the x-www-browser
# alternative, so sensible-browser and anything else that asks Debian for the
# browser lands in the fleet's Chromium too. Re-installed whenever the source
# in the tree differs from the installed copy.
readonly _BROWSER_OPEN_SOURCE="$REPO_ROOT/system/apps/screen/scripts/screen-browser-open"
readonly _BROWSER_OPEN_INSTALL_PATH="/usr/local/bin/screen-browser-open"
readonly _BROWSER_OPEN_ALTERNATIVE_PRIORITY=200

_install_browser_command() {
    local alternative
    alternative="$(update-alternatives --query x-www-browser 2>/dev/null | sed -n 's/^Value: //p')"
    if cmp -s "$_BROWSER_OPEN_SOURCE" "$_BROWSER_OPEN_INSTALL_PATH" \
        && [ "$alternative" = "$_BROWSER_OPEN_INSTALL_PATH" ]; then
        _log "browser command: already installed, satisfied"
        return 0
    fi
    if ! install -D -m 755 "$_BROWSER_OPEN_SOURCE" "$_BROWSER_OPEN_INSTALL_PATH"; then
        _log "browser command: could not install $_BROWSER_OPEN_INSTALL_PATH; the next converge retries"
        return 1
    fi
    if ! update-alternatives --install /usr/bin/x-www-browser x-www-browser \
        "$_BROWSER_OPEN_INSTALL_PATH" "$_BROWSER_OPEN_ALTERNATIVE_PRIORITY"; then
        _log "browser command: could not register the x-www-browser alternative; the next converge retries"
        return 1
    fi
    _log "browser command: install complete ($_BROWSER_OPEN_INSTALL_PATH)"
}

main() {
    local rc=0
    _install_screen_packages || rc=$?
    _install_neko || rc=$?
    _install_fortress || rc=$?
    _install_browser_command || rc=$?
    # Extensions are a nice-to-have: a failure here must not fail the unit, because a
    # browser without an ad blocker still works and the converge retries next boot.
    _install_extensions || _log "extensions: not installed this pass; will retry next converge"
    if [ "$rc" -eq 0 ]; then
        _log "unit satisfied"
    else
        _log "unit failed (exit $rc); see logs above"
    fi
    return "$rc"
}

main "$@"
