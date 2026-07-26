#!/bin/sh
# First-boot seed script for default-workspace-template containers.
#
# Run synchronously by mngr (via the `post_host_create_command` create-
# template hook) once the host is online but before any agent work_dir
# setup. Responsibilities:
#
#   1. Seed the /home/user persistent volume with the baked workspace at
#      ~/workspace using an atomic two-step move via /home/user/workspace.moving
#      so a crash mid-copy never leaves /home/user/workspace half-populated.
#      The workspace was baked into /home/user/workspace at image-build time
#      and then renamed to /docker_build_code at the very end of the build (so
#      the runtime /home/user volume mount path is empty in the shipped
#      image); this script relocates it onto the volume.
#
#   2. Clean up /docker_build_code after seeding succeeds, so it doesn't
#      keep occupying overlay space on the running container.
#
#   3. Seed the home skeleton on the volume: ~/worktrees, the ~/.cache ->
#      /var/cache/user symlink (so even tools that hardcode ~/.cache stay off
#      the volume and out of backups), a minimal ~/.bashrc (PATH + mngr env
#      sourcing -- root's dotfiles live on the volume now), and the
#      ~/.mngr/layout-version stamp so every backup self-describes its data
#      layout.
#
# This script is installed at /usr/local/bin/default-workspace-template-seed by an image-layer
# COPY (not via the volume-bound /home/user/) so that it is available
# before the seed step itself runs.
#
# Race-free: mngr blocks on this command's exit before issuing any other
# work that touches /home/user/. No PID-1 / signal-handling logic here -- the
# container's long-running CMD (mngr's generic keep-alive) handles that.

SEED_SOURCE=/docker_build_code
SEED_STAGING=/home/user/workspace.moving
SEED_TARGET=/home/user/workspace

seed_workspace_onto_volume() {
    # Warm-boot fast path: the workspace is already on the volume. Do not
    # touch it -- the agent may have made local edits we must not
    # overwrite.
    if [ -d "$SEED_TARGET" ] && [ -n "$(ls -A "$SEED_TARGET" 2>/dev/null)" ]; then
        return 0
    fi

    # A prior boot crashed between staging and the atomic rename. Wipe
    # the half-staged copy and re-stage from /docker_build_code below.
    if [ -e "$SEED_STAGING" ]; then
        echo "default-workspace-template-seed: wiping stale $SEED_STAGING from a prior interrupted seed"
        rm -rf "$SEED_STAGING"
    fi

    # Broken-volume case: the image's seed source is gone AND the volume
    # has neither the final nor the staged copy. Fail loudly so the
    # issue surfaces in mngr/docker logs, rather than the container
    # silently sleeping forever with no workspace.
    if [ ! -e "$SEED_SOURCE" ]; then
        echo "default-workspace-template-seed: ERROR: $SEED_TARGET missing AND $SEED_SOURCE missing -- volume is in a broken state and cannot be seeded" >&2
        exit 1
    fi

    # Stage: cross-filesystem copy from the image layer onto the volume.
    # `cp -a` preserves mode/owner/timestamps. Land on a sibling path so
    # the final rename below is a single inode-level operation on the
    # same filesystem.
    echo "default-workspace-template-seed: staging $SEED_SOURCE -> $SEED_STAGING"
    cp -a "$SEED_SOURCE" "$SEED_STAGING"

    # Remove any pre-existing empty target so the atomic mv below
    # replaces it cleanly. `mv src dst` when dst is an existing
    # directory moves src INTO dst (dst/src) rather than replacing dst,
    # which would land the workspace at /home/user/workspace/code.moving instead
    # of /home/user/workspace/. `rmdir` only succeeds on empty directories, so
    # this is safe -- we already early-returned above if the target was
    # non-empty.
    rmdir "$SEED_TARGET" 2>/dev/null || true

    # Commit: atomic rename. Either fully succeeds or doesn't happen at
    # all, so an interrupted seed either has the workspace fully in
    # place or still has /home/user/workspace.moving to re-stage from on the next
    # invocation.
    echo "default-workspace-template-seed: atomic-renaming $SEED_STAGING -> $SEED_TARGET"
    mv "$SEED_STAGING" "$SEED_TARGET"
}

cleanup_seed_source() {
    # Only safe to remove the image-layer source AFTER the volume target
    # is in place. Skip silently if a prior seed already cleaned it up.
    if [ -e "$SEED_SOURCE" ]; then
        echo "default-workspace-template-seed: cleaning up $SEED_SOURCE"
        rm -rf "$SEED_SOURCE"
    fi
}

seed_home_skeleton() {
    # Shared with the lima/modal provisioning path so every provider produces
    # the same home layout; the workspace copy is in place by this point.
    sh "$SEED_TARGET/scripts/seed_home_skeleton.sh"
}

set -e
seed_workspace_onto_volume
cleanup_seed_source
seed_home_skeleton
