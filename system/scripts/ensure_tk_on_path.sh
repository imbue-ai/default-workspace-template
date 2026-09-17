#!/usr/bin/env bash
# SessionStart hook: make `tk` (and `ticket`) callable without the full path.
# The workspace build links the vendored script into /usr/local/bin; this
# hook covers images built before that and local dev sessions, by linking it
# into ~/.local/bin when nothing on PATH already answers. A worker runs in a
# worktree under the same HOME, so a link written from there would point
# every shell in the workspace at a checkout that goes away with the worker.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tk_script="${repo_root}/system/vendor/tk/ticket"

[[ -x "$tk_script" ]] || exit 0

for name in tk ticket; do
    found="$(command -v "$name" || true)"
    if [[ -n "$found" && -x "$found" ]]; then
        continue
    fi
    mkdir -p "${HOME}/.local/bin"
    ln -sf "$tk_script" "${HOME}/.local/bin/${name}"
done
