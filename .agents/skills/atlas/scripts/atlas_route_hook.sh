#!/usr/bin/env bash
# Atlas prompt router -- NON-BLOCKING. Wired to UserPromptSubmit. Captures the
# submitted prompt and hands it to atlas_route.py, spawned DETACHED so the model
# classify call never adds latency to the prompt. The router decides whether the
# task belongs to an existing page, warrants a new (proposed) page, or is too
# small to track. It writes nothing to this hook's stdout, so nothing is injected
# into the agent's context here. Always exits 0.
# strict mode; the guards below use if/fi (not `&& exit`) so `-e` never trips on
# a false test.
set -euo pipefail

# Capture the hook's JSON stdin (contains the user's prompt) BEFORE anything else.
# Unlike the other hooks this one CAPTURES rather than drains -- it needs the prompt.
payload="$(cat 2>/dev/null || true)"

# Source the shared helpers; if they are absent (Atlas not installed here) no-op,
# so `set -e` never aborts on a missing source.
_atlas_common="${MNGR_AGENT_WORK_DIR:-$(pwd)}/.agents/skills/atlas/scripts/_atlas_hook_common.sh"
if [ ! -f "$_atlas_common" ]; then
    exit 0
fi
# shellcheck source=_atlas_hook_common.sh
. "$_atlas_common"

# Sub-agents / launch-task workers must not route (mirrors the other hooks).
if atlas_hook_is_subagent; then
    exit 0
fi

root="$(atlas_hook_root)"
router="${root}/.agents/skills/atlas/scripts/atlas_route.py"
# Gate only on the skill being present + a prompt to route -- NOT on atlas/topics/
# existing, so the router can create the very first page (it writes that dir).
if [ ! -f "$router" ] || [ -z "$payload" ]; then
    exit 0
fi

# Stash the payload in a throwaway file the detached router reads then unlinks.
statedir="${root}/data/.state/atlas"
mkdir -p "$statedir" 2>/dev/null || true

# Keep the error log from growing without bound.
log="${statedir}/route.log"
if [ -f "$log" ] && [ "$(wc -c <"$log" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    : >"$log"
fi

tmp="$(mktemp "${statedir}/route-payload.XXXXXX" 2>/dev/null || true)"
if [ -z "$tmp" ]; then
    exit 0
fi
if ! printf '%s' "$payload" >"$tmp" 2>/dev/null; then
    rm -f "$tmp"
    exit 0
fi

# Detach with nohup (portable to macOS and Linux, unlike setsid) + `&`, output
# discarded, so the prompt returns immediately and the classify call runs in the
# background surviving this hook's exit.
nohup python3 "$router" --input "$tmp" --repo-root "$root" \
    >/dev/null 2>>"$log" &

exit 0
