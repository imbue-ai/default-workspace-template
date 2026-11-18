#!/usr/bin/env bash
# SessionStart hook (claude and codex): bring the root venv up to date with the
# committed uv.lock, after making .venv a link to a target beside the uv cache
# (system/scripts/workspace_bare_scripts/venv_links.py).
#
# Nothing here relocks. In the shared main tree, a lock that has drifted from
# pyproject.toml usually means another agent is partway through a dependency
# change; regenerating it here would land that change in whichever session
# started next. So the sync is --frozen, and a drift is only reported. This
# hook's stdout reaches the agent as session context, so the note asks for
# nothing unless the agent is the one changing dependencies.
set -euo pipefail

# SessionStart also fires on resume, clear and compaction, when the agent's shell may have moved.
script_dir=$(cd "$(dirname "$0")" && pwd)
cd "${MNGR_AGENT_WORK_DIR:-${CLAUDE_PROJECT_DIR:-.}}"

if ! uv lock --check >/dev/null 2>&1; then
    echo "Note (no action needed unless you are changing dependencies): uv.lock is out of date with pyproject.toml, so the environment was synced from the committed lock. Whoever is changing dependencies runs \`uv lock\`, then \`uv sync --all-packages\`, and commits uv.lock with the change."
fi
python3 "$script_dir/workspace_bare_scripts/venv_links.py" ensure . || true
uv sync --all-packages --frozen
