#!/usr/bin/env bash
# env.d unit: the browser fleet's headful display + audio runtime -- Xvfb (each
# fleet browser runs headful under its OWN virtual display, see browser.display),
# xclip (X11 clipboard bridge for native copy/paste), the libva*/libgbm libs
# pixelflux (the live-view capture/encoder) links at import time even in CPU
# mode, and PulseAudio + ffmpeg (per-browser null sinks captured to PCM for the
# streamed audio, see browser.audio). Too heavy to bake into the Docker image
# and not required by any boot-time service (the xvfb/pulseaudio/browser
# supervisord programs retry until these land), so it installs on first boot
# via the `env-converge` one-shot.
#
# env.d contract: idempotent with a fast satisfied-check -- NO marker files.
# The converger re-runs every unit on every boot; a satisfied unit exits 0 in
# milliseconds (dpkg -s per pinned package), and version stability comes from
# the apt snapshot the workspace's sources point at.
set -euo pipefail

_log() {
    printf '[env.d/browser-display-audio] %s\n' "$*"
}

# Headful display + capture-encoder runtime. The libva set must be FULL
# (verified via `ldd` on the pixelflux wheel: libva2, libva-drm2, libva-x11-2,
# libgbm1, and libdrm2 which apt pulls transitively) -- without it,
# `import pixelflux` fails and the browser service can't stream the live view.
readonly _DISPLAY_PACKAGES=(xvfb xclip libva2 libva-drm2 libva-x11-2 libgbm1)
# Sound server + capture: each browser plays into its own PulseAudio null sink,
# which ffmpeg captures to PCM for the viewer stream.
readonly _AUDIO_PACKAGES=(pulseaudio pulseaudio-utils ffmpeg)

_recover_interrupted_dpkg() {
    # A prior apt/dpkg run killed mid-operation leaves dpkg broken, after which
    # every `apt-get install` aborts. This happens routinely for pool hosts: the
    # bake's `mngr stop` parks the host while this unit's first-boot `apt` is
    # still running, so the post-lease retry would otherwise fail forever.
    # Recover up front -- each step is a fast no-op when dpkg is already
    # consistent. Best-effort: we log failures loudly (not silently) and let the
    # apt step below surface the real error. Mirrors 1000-playwright-fortress.sh.
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
    if ! apt-get --fix-broken install -y; then
        _log "WARNING: 'apt-get --fix-broken install' returned non-zero; the apt install below may still fail"
    fi
}

_packages_installed() {
    local pkg
    for pkg in "$@"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || return 1
    done
    return 0
}

_install_packages() {
    # Fast satisfied-check: every package already registered with dpkg.
    if _packages_installed "${_DISPLAY_PACKAGES[@]}" "${_AUDIO_PACKAGES[@]}"; then
        _log "apt packages already installed, satisfied"
        return 0
    fi
    _recover_interrupted_dpkg
    _log "installing display + audio packages: ${_DISPLAY_PACKAGES[*]} ${_AUDIO_PACKAGES[*]}"
    if ! apt-get update -y; then
        _log "apt-get update FAILED; the next converge retries"
        return 1
    fi
    if ! apt-get install -y --no-install-recommends \
        "${_DISPLAY_PACKAGES[@]}" "${_AUDIO_PACKAGES[@]}"; then
        _log "apt install FAILED; the next converge retries"
        return 1
    fi
    _log "apt install complete"
}

_write_chromium_policy() {
    # Suppress Chromium's yellow "You are using an unsupported command-line flag:
    # --no-sandbox" banner. We MUST pass --no-sandbox (Chromium's sandbox needs user
    # namespaces we don't have running as root in-container), and the banner then shows in
    # the streamed view. CommandLineFlagSecurityWarningsEnabled=false is the documented
    # managed policy for exactly this -- it changes NO browser behavior (unlike --test-type,
    # which we avoid for stealth). Written to the standard Chromium managed-policy dirs;
    # idempotent and cheap (three tiny file writes), so it needs no satisfied-check. If
    # Fortress reads a different dir, the banner persists and we add that dir here.
    local written=0 dir
    for dir in /etc/chromium/policies/managed /etc/opt/chrome/policies/managed /etc/chromium-browser/policies/managed; do
        if mkdir -p "$dir" 2>/dev/null && \
           printf '{"CommandLineFlagSecurityWarningsEnabled": false}\n' > "$dir/minds-flag-warnings.json" 2>/dev/null; then
            written=1
        fi
    done
    [ "$written" -eq 1 ] && _log "chromium policy: flag-warning banner suppressed" || _log "chromium policy: could not write managed policy"
}

main() {
    local rc=0
    _write_chromium_policy
    _install_packages || rc=$?
    if [ "$rc" -eq 0 ]; then
        _log "unit satisfied"
    else
        _log "unit failed (exit $rc); see logs above"
    fi
    return "$rc"
}

main "$@"
