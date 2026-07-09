#!/usr/bin/env bash
# PreInvocation hook (antigravity): substitute for claude_update_plugin.sh's
# SessionStart -- antigravity has no SessionStart event. Runs once per agent
# lifetime (a marker file, not a hook-input invocation-count field -- that
# field's exact name was never independently confirmed against real hook
# input, so this avoids depending on it) rather than on every model call,
# since `agy plugin install` -- while confirmed idempotent -- does a live
# network fetch each time, which isn't worth paying on every turn.
#
# TEMPORARY: points at a personal fork + PR branch, same as
# codex_update_plugin.sh -- see that file's header for the full explanation.
# SWITCH BACK to the canonical imbue-ai/code-guardian repo once
# https://github.com/imbue-ai/code-guardian/pull/25 merges.
set -euo pipefail

cat > /dev/null 2>&1 || true

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
marker="${repo_root}/runtime/.antigravity_plugin_update_done"

if [[ -f "$marker" ]]; then
    echo '{}'
    exit 0
fi

# Only write the marker on real success -- an `|| true` here would swallow
# a transient failure (network blip, agy not yet authenticated) and mark it
# "done" forever, permanently skipping the install with no retry (found by
# code review). `if` exempts its own condition from `set -e`, so this needs
# no `|| true` to stay safe.
if agy plugin install "https://github.com/minhtrinh-imbue/code-guardian/tree/add-codex-opencode-antigravity-support/plugins/imbue-code-guardian-antigravity" >&2; then
    mkdir -p "$(dirname "$marker")"
    touch "$marker"
else
    echo "agy plugin install failed; will retry on the next invocation" >&2
fi

echo '{}'
