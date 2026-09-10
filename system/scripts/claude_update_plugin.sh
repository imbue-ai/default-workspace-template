#!/usr/bin/env bash
set -euo pipefail
#
# claude_update_plugin.sh [--strict]
#
# Install (for the current project path) and update the Claude Code plugins
# that .claude/settings.json enables, so their skills are loaded by the NEXT
# claude session started from this directory.
#
# Two callers, one contract:
#
#   - The SessionStart hook in .claude/settings.json runs it without --strict.
#     A plugin installed from inside a running session is only loaded by the
#     next session (Claude Code resolves the plugin set at startup), so the hook
#     converges state for later sessions and never blocks the current one.
#     Failures are reported on stdout -- SessionStart stdout is injected into
#     the session context -- so the agent can explain why a skill is missing.
#
#   - The worker create template (.mngr/settings.toml) runs it as a provision
#     command, BEFORE the worker's claude starts. A worker gets a single session
#     in a fresh worktree path, so this is the only place an install can land in
#     time for it. It deliberately runs without --strict: a plugin outage must
#     not make a worker undeployable, so a failure there is a warning in the
#     provision output, and the worker itself reports the missing gate.
#
#   --strict (exit 1 when any plugin failed to install) is for callers that
#   would rather fail fast than run without the plugins, e.g. the acceptance
#   test in test_claude_plugin_first_session.py.
#
# The cache is never wiped here. Every claude in the workspace shares one
# config dir, so deleting plugins/cache/<marketplace> while other sessions run
# strips their plugin skills and hook scripts; and a session whose reinstall
# then fails (offline, git auth) starts with no plugin at all.
#
# The scope MUST be project. `claude plugin install` defaults to *user* scope,
# which enables the plugin for every Claude session on the host -- including
# headless `claude -p` children that pass `--setting-sources user` (e.g.
# pr-review's dependency-install agent). A user-scoped code-guardian would then
# run its review/CI Stop hook against those children and block them. Keeping the
# plugins project-scoped confines them to this repo's own agents.

# "<plugin>@<marketplace>|<marketplace github repo>". Mirrors enabledPlugins +
# extraKnownMarketplaces in .claude/settings.json (claude_update_plugin_test.py
# pins the two in step). Claude Code only registers a project's
# extraKnownMarketplaces once a session has started in that project, which is
# exactly what has not happened yet at provision time, so the marketplace is
# registered here when missing.
PLUGINS=(
    "imbue-code-guardian@imbue-code-guardian|imbue-ai/code-guardian"
    "frontend-design@claude-code-plugins|anthropics/claude-code"
)

STRICT=0
if [[ "${1:-}" == "--strict" ]]; then
    STRICT=1
elif [[ $# -gt 0 ]]; then
    echo "usage: $0 [--strict]" >&2
    exit 2
fi

if ! command -v claude &>/dev/null; then
    if [[ "$STRICT" == "1" ]]; then
        echo "error: claude is not on PATH, so the plugins cannot be installed" >&2
        exit 1
    fi
    exit 0
fi

# Non-interactive ssh so a marketplace git fetch can never hang at a host-key
# or credential prompt, with a short connect timeout so every attempt finishes
# within the SessionStart hook's time budget even when fully offline.
export GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5'

# `claude plugin marketplace list` prints one "  > <name>" line per registered
# marketplace followed by a "Source: ..." line; keep only the name lines.
known_marketplaces=$(claude plugin marketplace list 2>/dev/null | grep -v "Source:" || true)

is_marketplace_known() {
    grep -qE "(^|[[:space:]])$1([[:space:]]|$)" <<< "$known_marketplaces"
}

failed_plugin_ids=()
for entry in "${PLUGINS[@]}"; do
    plugin_id="${entry%%|*}"
    marketplace_repo="${entry#*|}"
    marketplace="${plugin_id#*@}"

    if ! is_marketplace_known "$marketplace"; then
        if output=$(claude plugin marketplace add "$marketplace_repo" 2>&1); then
            printf '%s\n' "$output"
            known_marketplaces+=$'\n'"$marketplace"
        else
            printf '%s\n' "$output" >&2
            failed_plugin_ids+=("$plugin_id")
            continue
        fi
    fi

    # Install first: it records the plugin for THIS project path (a no-op when
    # already recorded), which is what makes a fresh worktree load it. Update
    # then refreshes the shared cache to the marketplace's latest version.
    if output=$(claude plugin install --scope project "$plugin_id" 2>&1); then
        printf '%s\n' "$output"
    else
        printf '%s\n' "$output" >&2
        failed_plugin_ids+=("$plugin_id")
        continue
    fi
    if output=$(claude plugin update --scope project "$plugin_id" 2>&1); then
        printf '%s\n' "$output"
    else
        # A failed update leaves the just-installed (or previously cached)
        # version in place, which is still a working plugin.
        printf '%s\n' "$output" >&2
        echo "warning: could not update plugin ${plugin_id}; using the cached version"
    fi
done

if [[ ${#failed_plugin_ids[@]} -gt 0 ]]; then
    echo "warning: failed to install plugin(s) ${failed_plugin_ids[*]} for $(pwd); sessions started here will lack their skills unless a cached copy is already loaded"
    if [[ "$STRICT" == "1" ]]; then
        exit 1
    fi
fi
