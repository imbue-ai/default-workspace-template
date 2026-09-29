A plan-node worker no longer runs `uv sync --all-packages` and `claude_update_plugin.sh` twice.
Its create template provisions the worktree with both, and then the SessionStart hooks in
`.claude/settings.json` ran them again as the Claude session opened. Both hooks now skip an agent
whose `MNGR_AGENT_ROLE` is `worktree_worker`, the same guard `shared_folder_worker` already had.

Measured on roadmap-parallel-v4-attempt2: each of its five workers spent 40 to 50 seconds between
the orchestrator issuing its launch and the worker's first recorded action, which is where the
duplicate work sat. Everything else in that window stays: the git worktree, the first sync, the
task file's runtime-dir copy, the Claude process start and its readiness wait, and the task message.
