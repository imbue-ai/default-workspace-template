Added `system/scripts/write_unused_plan.sh` and its instructions at
`system/scripts/unused_plan/SKILL.md`: the plan recorder the `build-app` skill
fires off. It records, for offline analysis, the plan a separate agent would
have written for the same request. Nothing in the workspace reads the result.

It is built to be invisible to the work going on around it:

- It returns immediately -- the first invocation re-execs itself detached, so
  the calling agent never waits and never sees a failure.

- The detached child is a plain headless claude, not an `mngr` agent. An
  `mngr create` agent would show up in the workspace's agent list (the UI hides
  only `is_primary=true` agents), would pay for provisioning on every
  `build-app` call, and would have to be destroyed afterwards.

- It runs with `--setting-sources user`, so none of this repo's project hooks
  fire for it -- in particular the SessionStart `uv sync --all-packages`, which
  would rebuild the venv underneath the agent that spawned it. That flag also
  suppresses project skill discovery, which is why the instructions are passed
  as the prompt rather than invoked as a slash command.

- Its tools are `Read,Grep,Glob`. The plan comes back on stdout and the script
  writes the file, so the child has no tool that can touch the workspace. The
  script prepends a fixed "do not use this plan" header, which therefore
  appears regardless of what the model did with its instructions.

- It unsets the spawning agent's `MNGR_*` and session environment, runs under
  `nice`, tags itself into oom_priority's `AGENT_SUBPROCESS` band, and is
  capped at 15 minutes.

`data/.imbue/plans/` (and a `README.md` for the `data/.imbue/` bucket it shares
with the analytics footprint) now ships with the template, and
`agent_require_steps_pretool.sh` exempts the recorder so it never nudges the
agent to declare a step for something the user must not be shown.
