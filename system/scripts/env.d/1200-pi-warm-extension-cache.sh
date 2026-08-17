#!/usr/bin/env bash
# env.d unit: pre-warm pi's extension transpile cache so the FIRST pi agent
# created after a boot is fast, not just the ones after it.
#
# Why this exists: pi loads its extensions (pi-subagents, pi-web-access) as
# TypeScript and transpiles them through jiti on every fresh session start.
# jiti caches the transpiled output under $TMPDIR/jiti, keyed by each source
# file's ABSOLUTE PATH. With agent_types.pi-coding.share_home_npm_dir = true the
# agents all read the extensions from the one shared ~/.pi/agent/npm tree, so
# that cache is reused across agents -- BUT $TMPDIR/jiti lives on the container
# rootfs and is wiped on every boot. Without a warm-up the first agent each boot
# pays the full ~16-24s transpile before it is ready; this runs pi once at boot
# so the cache is already populated when the user creates their first agent.
#
# It lives in env.d (which env-converge runs once per boot) rather than the
# first-boot seed (seed_home_skeleton.sh) precisely because the cache is
# boot-ephemeral -- a first-boot-only warm-up would silently stop helping after
# the next reboot. It only pairs with share_home_npm_dir: a per-agent npm COPY
# sits at a unique path, so the shared-path cache this warms would not match it.
#
# env.d contract: idempotent with a fast satisfied-check -- no markers.
set -euo pipefail

PI_DIR=/home/user/.pi/agent
JITI_CACHE="${TMPDIR:-/tmp}/jiti"

# Nothing to warm until pi is installed and its extensions are seeded (the
# first-boot seed populates ~/.pi/agent/npm; if it has not run yet, skip and let
# a later converge pass -- or the first real agent -- warm it).
command -v pi >/dev/null 2>&1 || { echo "[env.d/pi-warm-cache] pi not installed, skipping"; exit 0; }
[ -d "$PI_DIR/npm/node_modules" ] || { echo "[env.d/pi-warm-cache] pi extensions not seeded yet, skipping"; exit 0; }

# Satisfied-check: if the transpile cache already holds entries (a pi already ran
# this boot), there is nothing to do.
if [ -d "$JITI_CACHE" ] && compgen -G "$JITI_CACHE"/*.mjs >/dev/null 2>&1; then
    echo "[env.d/pi-warm-cache] jiti cache already warm, satisfied"
    exit 0
fi

echo "[env.d/pi-warm-cache] warming pi extension transpile cache..."
# One throwaway headless run: pi boots, transpiles the extensions into the jiti
# cache at the shared ~/.pi/agent/npm path (the exact path share_home_npm_dir
# agents resolve to, so the keys match), then exits. --no-session leaves nothing
# behind; a cheap model is pinned so the throwaway turn is near-free; the startup
# version check is skipped. Best-effort by design: pi transpiles the extensions
# during session start, BEFORE the model turn, so the cache warms even if the
# turn itself errors (e.g. no credits) -- a failed warm-up must never fail boot.
PI_CODING_AGENT_DIR="$PI_DIR" PI_SKIP_VERSION_CHECK=1 \
    timeout 90 pi -p 'ok' --no-session --model anthropic/claude-haiku-4-5 >/dev/null 2>&1 \
    || echo "[env.d/pi-warm-cache] warm-up turn exited non-zero (cache still warmed at startup); continuing"

exit 0
