`plan_orchestration.py parse` takes two more switches, both off by default, and `plan.json` gains
a `settings` block recording every switch a build ran with so the orchestrator reads its shape out
of the file rather than remembering it. Each node also gains an `agent`, naming the agent that runs
it, which is `node-<N>` by default.

`--shared-worktree` runs every worker in the integration folder the orchestrator already made and
synced in Step 2, through a new `shared_worker` create template whose `transfer` is `none` and
which `create_worker.py launch --work-folder` points at that folder. It removes a `git worktree
add` and a `uv sync --all-packages` per worker, and with it the branches and the merges: nobody
commits per node, so a node is done when its report lands. The trade is the one the worktree model
was introduced to fix -- two nodes writing one file overwrite each other instead of conflicting --
so the planner's file ownership has to hold. It has: no run so far has had a merge conflict.

`--tier-agents` keeps one agent per capability alive across every node of that capability, so a
node's `agent` reads `low`, `medium` or `high` and several nodes name the same one. The
orchestrator launches an agent the first time it meets it and sends later nodes to the agent
already running, which saves a cold start per node and lets the second node start from what the
first one learned -- in one measured run a worker spent 144 seconds reading 8 files before it wrote
anything. One agent takes its nodes one at a time, so this trades some parallelism for that.

Both worker roles keep everything the per-worktree worker had: the CLAUDE.md override that gives a
worker the worker rules instead of AGENTS.md (with the data-folder guidance and the `tk` rule they
carry), the review gates switched off, and the reviewer settings window. The SessionStart hooks in
`.claude/settings.json` now skip their `uv sync --all-packages` and `claude_update_plugin.sh` for
either role rather than only the worktree one.
