#!/usr/bin/env bash
# env.d unit: browser audio. Debian packages are pinned by the workspace's
# committed snapshot (and authenticated by apt); upstream artifacts are pinned
# by commit and SHA256 here.
set -euo pipefail

readonly DEST=/opt/kasm-audio
readonly RELAY_COMMIT=f056f949e79a55217ec44c8fc4c79418ada0c05e
readonly JSMPEG_COMMIT=924acfbd96fdf15e6748d1368a36d79d8f4cecf6
readonly JSMPEG_URL="https://raw.githubusercontent.com/phoboslab/jsmpeg/${JSMPEG_COMMIT}/jsmpeg.min.js"
readonly JSMPEG_SHA256=c0286ac82193fc9c9d5490ade425db04c446d9115bfe6823bf9bc0b0703c7760

log() { printf '[env.d/browser-audio] %s\n' "$*"; }

satisfied() {
    command -v pulseaudio >/dev/null && command -v pactl >/dev/null && command -v ffmpeg >/dev/null \
        && test -x "$DEST/kasm_audio_out-linux" && test -f "$DEST/jsmpeg.min.js" \
        && test "$(sha256sum "$DEST/jsmpeg.min.js" | awk '{print $1}')" = "$JSMPEG_SHA256"
}

main() {
    satisfied && { log 'already satisfied'; return; }
    apt-get update
    apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils ffmpeg ca-certificates curl

    local arch relay_url relay_sha tmp
    case "$(uname -m)" in
        x86_64) arch=amd64; relay_sha=657dae0690855caff7c2aa61f0dfa3ff20dd3e50276cd4c85c9f22c4e7a8ecf8 ;;
        aarch64) arch=arm64; relay_sha=cad4dc3c58c45f2e248c247e1b95af6807a857ee3a546f8b0f9037bd8e941903 ;;
        *) log "unsupported architecture: $(uname -m)"; return 1 ;;
    esac
    relay_url="https://kasmweb-build-artifacts.s3.amazonaws.com/kasm_websocket_relay/${RELAY_COMMIT}/kasm_websocket_relay_${arch}_develop.f056f9.tar.gz"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    curl -fsSL -o "$tmp/relay.tgz" "$relay_url"
    test "$(sha256sum "$tmp/relay.tgz" | awk '{print $1}')" = "$relay_sha"
    tar -xzf "$tmp/relay.tgz" -C "$tmp"
    curl -fsSL -o "$tmp/jsmpeg.min.js" "$JSMPEG_URL"
    test "$(sha256sum "$tmp/jsmpeg.min.js" | awk '{print $1}')" = "$JSMPEG_SHA256"
    install -d -m 0755 "$DEST"
    install -m 0755 "$tmp/kasm_websocket_relay/kasm_audio_out-linux" "$DEST/kasm_audio_out-linux"
    install -m 0644 "$tmp/jsmpeg.min.js" "$DEST/jsmpeg.min.js"
    satisfied
    log 'install complete'
}

main "$@"
