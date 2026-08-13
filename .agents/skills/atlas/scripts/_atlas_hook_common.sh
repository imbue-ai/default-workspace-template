#!/usr/bin/env bash
# Shared helpers for the Atlas hook wrappers (checkpoint, route, live-reminder,
# summary). This file is SOURCED, never executed. Each hook keeps its own
# `set -euo pipefail` (the meta-ratchet requires it per-file) and sources this for
# the parts they all repeat: the sub-agent guard, root resolution, the stdin
# drain, and the atlas-presence guard.
#
# Not folded in here: atlas_sweep.sh (a cron job with a different root fallback
# and no stdin/sub-agent handling).
set -euo pipefail

# True (0) when running inside a sub-agent / launch-task worker, which must never
# run the Atlas hooks. Use as: `if atlas_hook_is_subagent; then exit 0; fi`.
atlas_hook_is_subagent() {
    [ -n "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]
}

# The workspace root the hook operates on.
atlas_hook_root() {
    printf '%s' "${MNGR_AGENT_WORK_DIR:-$(pwd)}"
}

# Drain the hook's JSON stdin so the writer never blocks on a full pipe. (The
# route hook instead CAPTURES stdin, so it does not call this.)
atlas_hook_drain_stdin() {
    cat >/dev/null 2>/dev/null || true
}

# True (0) only when the Atlas script $1 and the presence directory $2 both exist
# -- so a workspace without the book (or without the skill) no-ops. The dir is a
# parameter because the book hooks require `<root>/atlas/topics` while the summary
# hook only requires `<root>/atlas`. Use as:
#   if ! atlas_hook_present "$engine" "${root}/atlas/topics"; then exit 0; fi
atlas_hook_present() {
    [ -f "$1" ] && [ -d "$2" ]
}
