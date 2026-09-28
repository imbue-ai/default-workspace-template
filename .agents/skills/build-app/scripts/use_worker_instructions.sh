#!/usr/bin/env bash
# Point this worktree's CLAUDE.md at the worker's own rules instead of AGENTS.md.
#
# A worker runs in a git worktree of the workspace repo, so it gets the repo's CLAUDE.md --
# whose first line is `@AGENTS.md`, pulling in 35 KB written for the agent that talks to the
# user. That is how a worker ended up being told to keep `tk` records, notify the user and
# follow a chat agent's conventions, none of which apply to it. The worker rules then had to
# open by telling it to disregard the file it had just been handed.
#
# So the import is repointed at those rules, which become the whole instruction set rather
# than a correction layered over a larger one.
#
# CLAUDE.md is tracked, and the worker commits its worktree for the orchestrator to merge, so
# the override must never reach a commit. `--skip-worktree` is what makes that true: git stops
# comparing the file, it stays out of `git status` and out of `git add -A`, and the committed
# CLAUDE.md remains the repo's. Verified rather than assumed -- see the test beside this script.
#
# Run from the worktree root, at provisioning time, before the worker's first turn.

set -euo pipefail

RULES=".agents/skills/build-app/references/worker-workspace-rules.md"

if [[ ! -f "$RULES" ]]; then
    echo "use_worker_instructions: $RULES is missing; leaving CLAUDE.md alone" >&2
    exit 0
fi

printf '@%s\n' "$RULES" > CLAUDE.md
git update-index --skip-worktree CLAUDE.md

echo "use_worker_instructions: CLAUDE.md now imports $RULES (skip-worktree set)"
