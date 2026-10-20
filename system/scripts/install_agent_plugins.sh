#!/bin/bash
# Install the agents' plugins, at pinned commits, as local marketplaces under
# /opt/agent-plugins. Idempotent like the other installers setup_system.sh
# runs: a marketplace already at its pinned commit is left in place.
#
#   imbue-code-guardian/         code-guardian's own repo. Its Claude and Codex
#                                marketplace manifests both live in it.
#   claude-code-plugins-pinned/  Anthropic's frontend-design plugin, in a
#                                marketplace of our own: Claude reserves the
#                                name "claude-code-plugins" for GitHub sources.
#
# .claude/settings.json declares both as directory marketplaces, and Claude
# loads their plugins in place, so no session or account installs anything.
# Codex agents install code-guardian from the local copy when provisioned
# (codex_update_plugin.sh). Nothing fetches plugins at runtime, so a plugin
# update reaches workspaces only through a pin bump here, which update-self
# applies by re-running setup_system.sh.
set -euo pipefail

: "${CODE_GUARDIAN_REPO:=https://github.com/imbue-ai/code-guardian.git}"
: "${CODE_GUARDIAN_COMMIT:=28a521b9df2046c4d0804a08eace5e3013bf34dd}"
: "${CLAUDE_CODE_REPO:=https://github.com/anthropics/claude-code.git}"
: "${CLAUDE_CODE_COMMIT:=94506ff0d3bd9185f1752da36e968775021fb48f}"
: "${AGENT_PLUGINS_ROOT:=/opt/agent-plugins}"

export GIT_TERMINAL_PROMPT=0

# Fetch one commit of `repo` into the new directory `dest`, limited to `paths`
# when any are given, with no git metadata left behind.
fetch_commit() {
    local repo="$1"
    local commit="$2"
    local dest="$3"
    shift 3
    git init -q "$dest"
    if [ "$#" -gt 0 ]; then
        git -C "$dest" sparse-checkout set --no-cone "$@"
    fi
    git -C "$dest" fetch -q --depth 1 "$repo" "$commit"
    git -C "$dest" checkout -q FETCH_HEAD
    rm -rf "$dest/.git"
}

# Swap `staged` in as `$AGENT_PLUGINS_ROOT/<name>`, recording its commit.
publish() {
    local name="$1"
    local commit="$2"
    local staged="$3"
    printf '%s\n' "$commit" > "$staged/.pinned-commit"
    rm -rf "$AGENT_PLUGINS_ROOT/.old-$name"
    if [ -e "$AGENT_PLUGINS_ROOT/$name" ]; then
        mv "$AGENT_PLUGINS_ROOT/$name" "$AGENT_PLUGINS_ROOT/.old-$name"
    fi
    mv "$staged" "$AGENT_PLUGINS_ROOT/$name"
    rm -rf "$AGENT_PLUGINS_ROOT/.old-$name"
}

is_current() {
    [ "$(cat "$AGENT_PLUGINS_ROOT/$1/.pinned-commit" 2>/dev/null || true)" = "$2" ]
}

mkdir -p "$AGENT_PLUGINS_ROOT"
tmp="$(mktemp -d "$AGENT_PLUGINS_ROOT/.staging.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

if ! is_current imbue-code-guardian "$CODE_GUARDIAN_COMMIT"; then
    fetch_commit "$CODE_GUARDIAN_REPO" "$CODE_GUARDIAN_COMMIT" "$tmp/imbue-code-guardian"
    publish imbue-code-guardian "$CODE_GUARDIAN_COMMIT" "$tmp/imbue-code-guardian"
fi

if ! is_current claude-code-plugins-pinned "$CLAUDE_CODE_COMMIT"; then
    fetch_commit "$CLAUDE_CODE_REPO" "$CLAUDE_CODE_COMMIT" "$tmp/claude-code" /plugins/frontend-design/
    staged="$tmp/claude-code-plugins-pinned"
    mkdir -p "$staged/.claude-plugin" "$staged/plugins"
    mv "$tmp/claude-code/plugins/frontend-design" "$staged/plugins/frontend-design"
    cat > "$staged/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "claude-code-plugins-pinned",
  "owner": {"name": "Anthropic"},
  "plugins": [{"name": "frontend-design", "source": "./plugins/frontend-design"}]
}
JSON
    publish claude-code-plugins-pinned "$CLAUDE_CODE_COMMIT" "$staged"
fi
