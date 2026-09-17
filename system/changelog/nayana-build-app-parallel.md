Added `build-app-parallel`: an app build that a planner splits into nodes and an
orchestrating agent carries out with several workers at once, in one shared
build folder. The skill (`.agents/skills/build-app-parallel/`) owns the
orchestration; `build-app` stays as the reference for how an app is built here
and is what the planner and the workers read.

The planner is the offline plan recorder's own planner, run in the foreground:
`system/scripts/imbue_plan_extra/write_plan.sh --run-dir <dir> <flow>` plans into
the caller's run directory, with no "do not use" header and a non-zero exit when
no plan was written. The recorder's detached mode is unchanged, except that a
failed planner's stdout is now kept in the run's log. `prompts/build-app-parallel.md`
is a copy of `prompts/build-app.md` that differs only where the new setup makes
it untrue (the plan is carried out, workers share one folder and own separate
files, the handoff is not a node), plus one rename: its reasoning block is
`<rationale>`, because every request asking for a `<thinking>` block was refused
by the API's `reasoning_extraction` safeguard -- which the recorder's own prompt
still is.

`[create_templates.shared_folder_worker]` in `.mngr/settings.toml` is the agent
those workers run as: `transfer = "none"` (the orchestrator passes
`--from :<folder>`), no per-worker provisioning, the stop hook off, and a prompt
that keeps each worker inside its own subtask. `create_worker.py launch` gains
`--work-folder` and `--model` for it, and the SessionStart `uv sync` in
`.claude/settings.json` skips that role, since the orchestrator syncs the folder
once.

**Local patch to vendored mngr**, in `api/create.py`: a failed `mngr create`
cleaned up by running `git worktree remove --force` on the agent's work dir --
including a work dir the caller handed it with `--transfer=none`, which is the
shared build folder holding every other worker's uncommitted work. The cleanup
now runs only for a work dir the create actually made. This has not been
submitted upstream: whether the flow keeps using one shared folder is still
open, so re-check this patch survives the next mngr refresh, and drop it if the
flow moves to a worktree per worker.
