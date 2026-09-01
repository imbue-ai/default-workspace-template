Added `system/scripts/imbue_plan_extra/`: the plan recorder the `build-app`
skill fires off, as a `write_plan.sh` wrapper plus the `SKILL.md` instructions
it feeds in. It records, for offline analysis, a routing plan for the same
request. Nothing in the workspace reads the result.

The plan always comes from Opus 5, named by its exact `claude-opus-5` id rather
than the `opus` alias: that alias resolved to Opus 4.8 until Claude Code
2.1.219, so an alias would leave the plan's model up to whichever CLI version a
workspace happens to be pinned to. Plans are compared with each other offline,
which they cannot be if they came from different models.

The plan is written in the conductor three-list form -- `capability`,
`subtasks`, and `access list`, one entry per step, up to five steps. Steps get
a capability bucket (`low` / `medium` / `high`) rather than a named model, on
the assumption of a suite of general-intelligence models differing only in
capability and cost. Each step's access list names the earlier steps it sees,
so steps with empty access lists are independent and can run in parallel. The
plan writer reads `build-app` first and routes the work that skill describes.

Each call gets its own directory,
`data/.imbue/plans/<utc-timestamp>-<agent>/`, holding `request.txt`, `plan.md`,
a `meta.json` of ids and timings, and that run's `log`. The wrapper prints the
directory it created as its only output, so a `write_plan.sh` call in an
agent's transcript maps straight to its results.

The recorder is built to be invisible to the work going on around it:

- It returns immediately -- the first invocation re-execs itself detached, so
  the calling agent never waits and never sees a failure.

- The detached child is a plain headless claude, not an `mngr` agent. An
  `mngr create` agent would show up in the workspace's agent list (the UI hides
  only `is_primary=true` agents), would pay for provisioning on every
  `build-app` call, and would have to be destroyed afterwards.

- It runs with `--setting-sources user`, so none of this repo's project hooks
  fire for it -- in particular the SessionStart `uv sync --all-packages`, which
  would rebuild the venv underneath the agent that spawned it. That flag also
  suppresses project skill discovery, which is why the instructions are fed in
  as the prompt rather than invoked as a slash command.

- Its tools are `Read,Grep,Glob`. The plan comes back on stdout and the script
  writes the file, so the child has no tool that can touch the workspace. The
  script prepends a fixed "do not use this plan" header, which therefore
  appears regardless of what the model did with its instructions.

- It unsets the spawning agent's `MNGR_*` and session environment, runs under
  `nice`, tags itself into oom_priority's `AGENT_SUBPROCESS` band, and is
  capped at 15 minutes.

`data/.imbue/plans/` now ships with the template, described in `data/README.md`
alongside the analytics footprint it shares the `data/.imbue/` bucket with. And
`agent_require_steps_pretool.sh` exempts the recorder, so it never nudges the
agent to declare a step for something the user must not be shown.
