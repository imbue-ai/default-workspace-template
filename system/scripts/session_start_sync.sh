#!/usr/bin/env bash
# SessionStart hook: bring the root venv up to date with uv.lock.
#
# The sync is --frozen, so nothing later in the session (every one-off runs
# `uv run --no-sync`) can relock behind the agent's back. A lock that has
# drifted from pyproject.toml is regenerated here instead, once, and the agent
# is told so; this hook's stdout reaches the agent as session context, so the
# note is worded to wait for the next commit rather than prompt action now.
set -euo pipefail

if ! uv lock --check >/dev/null 2>&1; then
    if uv lock --quiet; then
        echo "Note for your next commit (no action needed now): uv.lock was out of date with pyproject.toml and has been regenerated; include it in your next commit."
    else
        echo "Note (no action needed now): uv.lock is out of date with pyproject.toml and \`uv lock\` failed to regenerate it; the environment was synced from the existing lock. Run \`uv lock\` to see why before relying on a dependency change."
    fi
fi
uv sync --all-packages --frozen
