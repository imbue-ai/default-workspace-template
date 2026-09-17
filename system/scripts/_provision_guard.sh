#!/usr/bin/env bash
# Content-addressed provisioning skip guard (issue 2306).
#
# A provisioning step whose effects are GLOBAL (system packages, /usr/local/bin
# binaries, /root/.local tools) is a pure function of the repo content: running
# it again on the *identical* tree reproduces the same VM state. So we fingerprint
# the repo by its git tree hash and drop a marker once the step completes; a later
# run whose tree matches the marker skips it. The pre-baked Lima image's bake runs
# the step on the release tree, so a create that boots that image for the *same*
# tree finds the marker and skips it.
#
# IMPORTANT -- only guard steps with GLOBAL effects (e.g. setup_system). Do NOT
# guard steps that write outputs INTO the workspace repo (install_dependencies ->
# .venv/node_modules, build_workspace -> frontend dist): the create re-materializes
# /home/user/workspace from a git-mirror push (tracked files only), so those in-repo outputs
# are absent at create time and MUST be regenerated every create. Skipping them
# leaves the workspace half-built (e.g. "Frontend not built").
#
# Safe by construction -- it NEVER skips unless it can prove the identical tree
# was already provisioned:
#   * no git repo at the canonical path (e.g. the Docker build, which runs these
#     before any repo exists), not a git tree, or no marker  -> run normally.
#   * the tree hash covers the scripts themselves plus every lockfile and
#     vendored file, so any content change invalidates the marker.
#
# Usage (source it, then):
#   provision_skip_if_done <name>   # early-exits the calling script when matched
#   provision_mark_done <name>      # call at the end, after a successful run
#
# PROVISION_FORCE=1 runs the step even when the marker matches. The update
# apply sets it on every provisioner run: its rollback re-runs the provisioner
# from the restored tree to put the global toolchain back, and that tree is
# exactly the one whose marker was written when it was first provisioned; and
# the merged tree's own marker outlives that rollback, so a retry of the same
# merge would otherwise skip the run that installs the merged pins.

# Strict mode. This file is sourced by callers that already set this (e.g.
# setup_system.sh), so re-asserting it here is a no-op for them and keeps the
# library safe to source from anywhere.
set -euo pipefail

# Canonical workspace repo location in the VM: the Lima bake clones here and a
# create syncs the workspace here. Overridable for tests.
_PROVISION_REPO_ROOT="${PROVISION_REPO_ROOT:-/home/user/workspace}"
_PROVISION_MARKER_DIR="${PROVISION_MARKER_DIR:-/var/lib/minds/provision}"

_provision_tree_fingerprint() {
    git -C "$_PROVISION_REPO_ROOT" rev-parse "HEAD^{tree}" 2>/dev/null || true
}

provision_skip_if_done() {
    _name="$1"
    _fp="$(_provision_tree_fingerprint)"
    # No resolvable tree -> we cannot prove a match, so let the caller run.
    [ -n "$_fp" ] || return 0
    if [ -f "$_PROVISION_MARKER_DIR/$_fp.$_name.done" ]; then
        if [ "${PROVISION_FORCE:-}" = "1" ]; then
            echo "[provision-guard] $_name already provisioned for tree $_fp, but PROVISION_FORCE=1: running it again."
            return 0
        fi
        echo "[provision-guard] $_name already provisioned for tree $_fp; skipping."
        exit 0
    fi
}

provision_mark_done() {
    _name="$1"
    _fp="$(_provision_tree_fingerprint)"
    [ -n "$_fp" ] || return 0
    mkdir -p "$_PROVISION_MARKER_DIR"
    : > "$_PROVISION_MARKER_DIR/$_fp.$_name.done"
}

# The version pins a guarded step reads are `:=` defaults, so an exported
# *_VERSION in the caller's environment wins over the tree's. An image built
# before minds-v0.4.3 exported its pins as ENV, and every process in such a
# container still inherits the image's versions; a by-hand re-provision there
# reinstalls the old version and passes the step's own pin check. Drop them
# all before the defaults are read, unless the caller says the override is
# deliberate (PROVISION_PIN_OVERRIDE=1).
provision_drop_inherited_pins() {
    if [ "${PROVISION_PIN_OVERRIDE:-}" = "1" ]; then
        return 0
    fi
    _name=""
    for _name in $(compgen -A export | grep '_VERSION$' || true); do
        echo "[provision-guard] ignoring inherited $_name=${!_name}; the tree's pin applies (PROVISION_PIN_OVERRIDE=1 keeps it)."
        unset "$_name"
    done
}
