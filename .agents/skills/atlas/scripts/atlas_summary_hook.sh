#!/usr/bin/env bash
# Atlas Summary hook -- wired to Stop. When a large task just finished and the
# summary feature is on, this feeds a one-time nudge back to the agent (exit 2)
# so it ends its reply with a brief plain-English "what changed" summary for the
# user (design "Option A" -- the agent is the only thing that can post to chat).
# Otherwise it exits 0 and the turn ends normally.
#
# strict mode; guards use if/fi (not `&& exit`) so `-e` never trips on a false test.
set -euo pipefail

# Source the shared helpers; if they are absent (Atlas not installed here) no-op,
# so `set -e` never aborts on a missing source.
_atlas_common="${MNGR_AGENT_WORK_DIR:-$(pwd)}/.agents/skills/atlas/scripts/_atlas_hook_common.sh"
if [ ! -f "$_atlas_common" ]; then
    exit 0
fi
# shellcheck source=_atlas_hook_common.sh
. "$_atlas_common"

# Drain the hook's JSON stdin so the writer never blocks on a full pipe.
atlas_hook_drain_stdin

# Sub-agents / launch-task workers must not summarize (mirrors the other hooks).
if atlas_hook_is_subagent; then
    exit 0
fi

root="$(atlas_hook_root)"
script="${root}/.agents/skills/atlas/scripts/atlas_summary.py"
# The summary is topic-independent, so its presence guard checks <root>/atlas
# (not <root>/atlas/topics like the book hooks).
if ! atlas_hook_present "$script" "${root}/atlas"; then
    exit 0
fi

# The script prints the nudge iff a large task is due (and advances its baseline);
# it never fails the turn, hence `|| true`.
nudge="$(timeout 8 python3 "$script" --check --repo-root "$root" 2>/dev/null || true)"
if [ -n "$nudge" ]; then
    # exit 2 + stderr re-engages the agent with this instruction (Stop-hook contract).
    printf '%s\n' "$nudge" >&2
    exit 2
fi

exit 0
