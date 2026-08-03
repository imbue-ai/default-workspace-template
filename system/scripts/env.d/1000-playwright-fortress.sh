#!/usr/bin/env bash
# env.d unit: Fortress (stealth Chromium) + its apt system libs. Too heavy to
# bake into the Docker image and not required by any boot-time service, so it
# installs on first boot via the `env-converge` one-shot.
#
# env.d contract: idempotent with a fast satisfied-check -- NO marker files.
# The converger re-runs every unit on every boot; a satisfied unit exits 0 in
# milliseconds, and version stability comes from the pins in this script (a
# re-run never silently changes versions -- only a pin bump, landed via
# update-self, does).
set -euo pipefail

readonly REPO_ROOT="${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}"

_log() {
    printf '[env.d/playwright-fortress] %s\n' "$*"
}

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

# Fortress (tiliondev/fortress) stealth Chromium engine, replacing vanilla
# Playwright-managed Chromium. x64 from the official release; arm64 has no
# official release yet (tiliondev/fortress#29, open as of 2026-07-23) so this
# points at a fork build in the meantime -- swap _FORTRESS_ARM64_URL/_SHA256
# to the official tiliondev/fortress release once that PR merges.
# Fork build: https://github.com/minhtrinh-imbue/fortress/releases/tag/linux-arm64-151.0.7908.0-debian12
# PR:         https://github.com/tiliondev/fortress/pull/29
readonly _FORTRESS_X64_URL="https://github.com/tiliondev/fortress/releases/download/v151.0.7908.0/tilion-fortress-linux-x64.tar.gz"
readonly _FORTRESS_X64_SHA256="243238b2b8a8b944b7ba2b63533d2b917da7d569dcb290ce96bf28151294b873"
readonly _FORTRESS_ARM64_URL="https://github.com/minhtrinh-imbue/fortress/releases/download/linux-arm64-151.0.7908.0-debian12/tilion-fortress-linux-arm64-debian12.tar.gz"
readonly _FORTRESS_ARM64_SHA256="da6965af8fa8e995d137bcabdca8d163fde7f32ba483eaf5e029223995f19ada"
readonly _FORTRESS_INSTALL_DIR="/opt/fortress"

_install_fortress() {
    # Fast satisfied-check: the pinned engine binary is already in place (its
    # apt system libs were installed in the same pass that produced it).
    if [ -x "$_FORTRESS_INSTALL_DIR/tilion-fortress/tilion" ]; then
        _log "fortress: already installed at $_FORTRESS_INSTALL_DIR, satisfied"
        return 0
    fi
    # `install-deps` apt-installs the shared libs Chromium needs (libnss3,
    # libgbm, etc.) -- Fortress is a Chromium build too, same requirement.
    # Recover any interrupted dpkg state first so a bake-interrupted install
    # can actually complete on retry.
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
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_dir'" RETURN
    local asset="$tmp_dir/fortress.tar.gz"
    if ! curl -fsSL -o "$asset" "$url"; then
        _log "fortress: download FAILED; the next converge retries"
        return 1
    fi
    if [ "$(sha256sum "$asset" | awk '{print $1}')" != "$sha256" ]; then
        _log "fortress: SHA256 mismatch -- refusing to install"
        return 1
    fi
    rm -rf "$_FORTRESS_INSTALL_DIR"
    mkdir -p "$_FORTRESS_INSTALL_DIR"
    if ! tar xzf "$asset" -C "$_FORTRESS_INSTALL_DIR"; then
        _log "fortress: extract FAILED; the next converge retries"
        return 1
    fi
    chmod +x "$_FORTRESS_INSTALL_DIR/tilion-fortress/tilion"
    # Point Playwright's DEFAULT chromium at Fortress too. A bare `chromium.launch()`
    # (no executable_path) looks in Playwright's own browser cache, which this install
    # deliberately leaves empty (we run `install-deps`, not `install`, so no managed
    # Chromium is downloaded). Symlink Fortress into that expected path -- resolved
    # from Playwright itself so it tracks the pinned version's revision/layout -- so
    # ad-hoc Playwright calls use the same one engine instead of erroring on a missing
    # build. Chromium finds its resources via /proc/self/exe (the real Fortress dir),
    # so symlinking just the binary is enough.
    local pw_chrome
    pw_chrome="$(cd "$REPO_ROOT" && uv run python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()' 2>/dev/null)"
    if [ -n "$pw_chrome" ]; then
        mkdir -p "$(dirname "$pw_chrome")"
        ln -sf "$_FORTRESS_INSTALL_DIR/tilion-fortress/tilion" "$pw_chrome"
        # Some Playwright versions gate launch() on a per-browser install marker.
        touch "$(dirname "$(dirname "$pw_chrome")")/INSTALLATION_COMPLETE" 2>/dev/null || true
        _log "fortress: pointed Playwright's default chromium at Fortress ($pw_chrome)"
    else
        _log "fortress: WARNING could not resolve Playwright's chromium path; a bare chromium.launch() will still need an explicit executable_path"
    fi
    _log "fortress: install complete (${_FORTRESS_INSTALL_DIR}/tilion-fortress/tilion)"
}

_install_xvfb() {
    # Fast satisfied-check (env.d contract: no marker files): both binaries exist.
    if command -v Xvfb >/dev/null 2>&1 && command -v xclip >/dev/null 2>&1; then
        _log "xvfb: Xvfb and xclip already installed, satisfied"
        return 0
    fi
    # Headful Chromium needs a display; Xvfb is a headless X server that gives it
    # one (the browser runs headful under it -- see session.py's _HEADLESS). xclip
    # bridges the resulting X11 clipboard to/from the user for native copy/paste
    # (images included). Recover any interrupted dpkg first, same as fortress.
    _recover_interrupted_dpkg
    _log "xvfb: installing xvfb + xclip"
    if apt-get update -y && apt-get install -y --no-install-recommends xvfb xclip; then
        _log "xvfb: install complete"
    else
        _log "xvfb: install FAILED; the next converge retries"
        return 1
    fi
}

main() {
    local rc=0
    _install_fortress || rc=$?
    _install_xvfb || rc=$?
    if [ "$rc" -eq 0 ]; then
        _log "unit satisfied"
    else
        _log "unit failed (exit $rc); see logs above"
    fi
    return "$rc"
}

main "$@"
