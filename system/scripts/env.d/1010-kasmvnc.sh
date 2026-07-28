#!/usr/bin/env bash
# env.d unit: KasmVNC (Xvnc + its bundled web client), the live-view transport for
# the browser fleet. A ~30 MB .deb, not needed by any boot-time service, so it
# installs on first boot via the `env-converge` one-shot like Fortress does.
#
# env.d contract: idempotent with a fast satisfied-check -- NO marker files.
# The converger re-runs every unit on every boot; a satisfied unit exits 0 in
# milliseconds, and version stability comes from the pins below.
#
# Why KasmVNC and not a hand-rolled capture: Xvnc IS the X server (a TigerVNC
# fork), so it replaces Xvfb rather than sitting on top of it, and it carries its
# own encoder, web server, HTML5 client, and -- the point -- the RFB input path.
# Mouse and keyboard arrive as X events with no code on our side.
set -euo pipefail

readonly REPO_ROOT="${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}"

_log() {
    printf '[env.d/kasmvnc] %s\n' "$*"
}

# Trixie builds, matching the workspace base image (system/Dockerfile is
# `FROM python:3.12-slim-trixie`). A bookworm .deb resolves against the wrong
# libt64 ABI and will not install here.
readonly _KASMVNC_VERSION="1.4.0"
readonly _KASMVNC_AMD64_URL="https://github.com/kasmtech/KasmVNC/releases/download/v1.4.0/kasmvncserver_trixie_1.4.0_amd64.deb"
readonly _KASMVNC_AMD64_SHA256="7b1515fbb0dd5002db2bcfd352882836333ac995dc423f440c8fbb9de30d7c31"
readonly _KASMVNC_ARM64_URL="https://github.com/kasmtech/KasmVNC/releases/download/v1.4.0/kasmvncserver_trixie_1.4.0_arm64.deb"
readonly _KASMVNC_ARM64_SHA256="aa7b20b69e239e0c6f50883e90665c24627a27fd9b3d37a421239ef5b8dfadae"

# Xvnc hard-requires xkbcomp at startup and dies without a default font, neither of
# which a slim base image carries. They are pulled as .deb dependencies, but named
# here too so the satisfied-check can't pass on a half-installed tree.
readonly _KASMVNC_RUNTIME_BINARIES=("Xvnc" "xkbcomp")

_recover_interrupted_dpkg() {
    # Identical in purpose to the Fortress unit's copy: a bake's `mngr stop` can park
    # the host mid-`apt`, leaving packages unpacked-but-unconfigured, or worse in
    # dpkg's reinst-required ("R") state, which `dpkg --configure -a` CANNOT repair
    # (it skips them) -- those must be reinstalled. Each step is a fast no-op when
    # dpkg is already consistent.
    if ! dpkg --configure -a; then
        _log "WARNING: 'dpkg --configure -a' returned non-zero; continuing with broken-package repair"
    fi
    local reinst_required
    reinst_required="$(dpkg-query -W -f '${Package} ${db:Status-Abbrev}\n' 2>/dev/null \
        | awk 'substr($2, 3, 1) == "R" { print $1 }')"
    if [ -n "$reinst_required" ]; then
        # shellcheck disable=SC2086
        _log "reinstalling packages left reinst-required by an interrupted unpack: $(echo $reinst_required | tr '\n' ' ')"
        # shellcheck disable=SC2086
        if ! apt-get install --reinstall -y $reinst_required; then
            _log "WARNING: reinstall returned non-zero; the install below may still fail"
        fi
    fi
    if ! apt-get --fix-broken install -y; then
        _log "WARNING: 'apt-get --fix-broken install' returned non-zero; the install below may still fail"
    fi
}

_is_satisfied() {
    # Fast satisfied-check: the pinned version is installed AND every binary we
    # actually invoke resolves. Checking the binaries (not just dpkg's status)
    # catches a partially-unpacked tree that dpkg still reports as installed.
    local installed
    installed="$(dpkg-query -W -f '${Version}' kasmvncserver 2>/dev/null || true)"
    case "$installed" in
        "$_KASMVNC_VERSION"*) ;;
        *) return 1 ;;
    esac
    local binary
    for binary in "${_KASMVNC_RUNTIME_BINARIES[@]}"; do
        command -v "$binary" >/dev/null 2>&1 || return 1
    done
    return 0
}

_install_kasmvnc() {
    if _is_satisfied; then
        _log "kasmvnc: ${_KASMVNC_VERSION} already installed, satisfied"
        return 0
    fi

    local url sha256
    case "$(uname -m)" in
        x86_64) url="$_KASMVNC_AMD64_URL"; sha256="$_KASMVNC_AMD64_SHA256" ;;
        aarch64) url="$_KASMVNC_ARM64_URL"; sha256="$_KASMVNC_ARM64_SHA256" ;;
        *) _log "kasmvnc: unsupported architecture $(uname -m)"; return 1 ;;
    esac

    _recover_interrupted_dpkg

    local tmp_dir
    tmp_dir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_dir'" RETURN
    local asset="$tmp_dir/kasmvncserver.deb"
    _log "kasmvnc: downloading $url"
    if ! curl -fsSL -o "$asset" "$url"; then
        _log "kasmvnc: download FAILED; the next converge retries"
        return 1
    fi
    if [ "$(sha256sum "$asset" | awk '{print $1}')" != "$sha256" ]; then
        _log "kasmvnc: SHA256 mismatch -- refusing to install"
        return 1
    fi

    # `apt-get install ./file.deb`, NOT `dpkg -i`: the package pulls xkbcomp,
    # xkb-data, xauth and libxfont2, and apt resolves them from the pinned
    # snapshot mirror. `dpkg -i` would leave them unmet and Xvnc would fail at
    # startup (no xkbcomp = no keymap = immediate exit).
    _log "kasmvnc: installing ${_KASMVNC_VERSION} and its apt dependencies"
    if ! apt-get update; then
        _log "kasmvnc: apt-get update FAILED; the next converge retries"
        return 1
    fi
    if ! apt-get install -y --no-install-recommends "$asset" xfonts-base; then
        _log "kasmvnc: apt install FAILED; the next converge retries"
        return 1
    fi

    if ! _is_satisfied; then
        _log "kasmvnc: install completed but the satisfied-check still fails"
        return 1
    fi
    _log "kasmvnc: install complete ($(command -v Xvnc))"
}

main() {
    local rc=0
    _install_kasmvnc || rc=$?
    if [ "$rc" -eq 0 ]; then
        _log "unit satisfied"
    else
        _log "unit failed (exit $rc); see logs above"
    fi
    return "$rc"
}

main "$@"
