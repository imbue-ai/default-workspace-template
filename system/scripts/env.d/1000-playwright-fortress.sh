#!/usr/bin/env bash
# env.d unit: Fortress (stealth Chromium) + its apt system libs. Too heavy to
# bake into the Docker image, and no boot-time service execs it directly (the
# browser service degrades gracefully until the engine arrives), so it installs
# on first boot via the `env-converge` one-shot. xvfb + xclip are baked into
# the image (setup_system.sh) because [program:xvfb] DOES exec its binary
# directly at boot; the install step below remains only for rootfses built
# from pre-bake images, where its satisfied-check is not yet instant.
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

# Environments that never use the browser stack (e.g. the Imbue Studio CI snapshot
# producer) opt out of the whole unit -- most importantly the hundreds-of-MB
# Fortress download -- by setting DWT_SKIP_BROWSER_UNIT=1 in the agent
# environment. An env var rather than a marker file keeps the env.d contract:
# the decision is re-evaluated every boot, so a workspace whose environment
# stops setting it simply converges the browser stack on its next boot.
if [ "${DWT_SKIP_BROWSER_UNIT:-}" = "1" ]; then
    _log "DWT_SKIP_BROWSER_UNIT=1 -- skipping the browser stack install"
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

# Distrust the eight eMudhra intermediates whose private keys were exposed
# (Mozilla bug 2083449; the same keys electron/electron#55126 blocks). They chain
# to emSign ECC Root CA - G3, which the pinned Fortress still ships in its root
# store, and to a Sectigo AAA Certificate Services cross-sign; Playwright launches
# with --disable-component-update, so Chrome's CRLSet never reaches Fortress.
# Fortress is a Chromium build (is_chrome_branded = false), so it reads machine
# policy from /etc/chromium/policies/managed/. CADistrustedCertificates (M132+,
# applied live) takes base64 DER certificates and distrusts any certificate with
# the same public key, the Sectigo cross-signs included. Written whether or not
# Fortress is installed yet, so a workspace that already has it gets the policy.
# CLEANUP: remove once the pinned Fortress is built on a Chromium whose root
# store and SPKI blocklist exclude these keys (158.0.8115.0 or later).
readonly _CHROMIUM_POLICY_DIR="/etc/chromium/policies/managed"
readonly _DISTRUST_POLICY="${_CHROMIUM_POLICY_DIR}/distrust-compromised-emudhra-intermediates.json"

_install_distrust_policy() {
    # main() calls this behind `||`, which turns off `set -e` in here, so every
    # step checks its own result.
    if ! mkdir -p "$_CHROMIUM_POLICY_DIR"; then
        _log "policy: could not create $_CHROMIUM_POLICY_DIR; the next converge retries"
        return 1
    fi
    # Staged in the parent directory: Chromium parses every file in managed/,
    # whatever its name, so a half-written temp file must never sit there.
    local tmp
    if ! tmp="$(mktemp "$(dirname "$_CHROMIUM_POLICY_DIR")/.distrust-policy.XXXXXX")"; then
        _log "policy: could not stage the distrusted CA policy; the next converge retries"
        return 1
    fi
    if ! cat > "$tmp" <<'POLICY'
{
  "CADistrustedCertificates": [
    "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"
  ]
}
POLICY
    then
        rm -f "$tmp"
        _log "policy: could not write the distrusted CA policy; the next converge retries"
        return 1
    fi
    if cmp -s "$tmp" "$_DISTRUST_POLICY"; then
        rm -f "$tmp"
        _log "policy: distrusted CA policy already in place, satisfied"
        return 0
    fi
    if ! { chmod 0644 "$tmp" && mv -f "$tmp" "$_DISTRUST_POLICY"; }; then
        rm -f "$tmp"
        _log "policy: could not install $_DISTRUST_POLICY; the next converge retries"
        return 1
    fi
    _log "policy: wrote $_DISTRUST_POLICY"
}

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

# Unpacked Chrome extensions loaded into every fleet browser, PINNED by version.
#
# These used to be downloaded at RUNTIME by browser-use, straight from the Chrome Web
# Store, unpinned -- so every workspace got whatever CWS served that day, loaded into the
# browser holding the user's real logins, on a first-launch network call. That violates
# this unit's own contract (versions come from pins here, never from a re-run), so they
# are pinned and installed here instead and passed via --load-extension (see chrome_args).
#
# uBlock Origin Lite keeps ad noise out of the accessibility snapshots the agent reads;
# "I still don't care about cookies" clears the consent walls it would otherwise have to
# click through. browser-use also shipped "Force Background Tab" -- deliberately dropped,
# because opening links in background tabs fights the pane's active-tab follow.
#
# NOT version-pinned, deliberately. The CRX endpoint only serves the CURRENT build for an
# id, so a "pin" here could only ever be a post-hoc check that logged a mismatch -- pinning
# theatre, not a pin. Freezing these for real would mean vendoring the CRX into the image or
# mirroring it, which is not worth it for two ad/cookie blockers. What this DOES fix versus
# browser-use is the timing and the ownership: the fetch happens once at converge, not on a
# user's first browser launch, and the set is chosen here rather than by a dependency.
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
        _log "extensions: download FAILED for ${name}; browsers run without it"
        return 1
    fi
    mkdir -p "$dest"
    # A .crx is a zip with a signature header; unzip skips the header and warns.
    if ! unzip -qo "$crx" -d "$dest" 2>/dev/null; then
        _log "extensions: unpack FAILED for ${name}; browsers run without it"
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
    command -v unzip >/dev/null 2>&1 || apt-get install -y --no-install-recommends unzip || return 1
    mkdir -p "$_EXTENSIONS_DIR"
    local rc=0
    _install_one_extension "ublock-origin-lite" "$_UBLOCK_ID" || rc=$?
    _install_one_extension "i-still-dont-care-about-cookies" "$_COOKIES_ID" || rc=$?
    return "$rc"
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
    _install_distrust_policy || rc=$?
    _install_fortress || rc=$?
    _install_xvfb || rc=$?
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
