# automations

The machinery that runs **automations** -- skills run automatically on a
schedule (see the workspace vocabulary in the root README). The weekly
Caretaker (`system/services/caretaker/`) is the built-in example; a new
automation needs only a skill plus a cron entry through these scripts.

Every cron job is prefixed with `system/scripts/with_agent_env.sh`, which
rebuilds the agent environment cron scrubs. It lives outside this package
because sshd scrubs the environment the same way, so commands sent over ssh
need it too; `with_agent_env.sh` here is a shim kept only for cron entries
scheduled before the move.

The pieces, each invoked from cron by absolute path:

- `run_job.sh` -- durable, completion-tracked runner for recurring jobs.
  Invoked every minute by a cron line; runs the given command at most once per
  interval (`--every 15m` / `3h` / `7d`, optional `--at <hour>`), catches up
  after downtime, and retries a run that failed or was killed mid-flight. A run
  only counts once it completes. State lives under `data/.state/jobs/<job-id>/`.
- `run_automation.sh` -- wakes a singleton "automation agent" for one run:
  creates it on the first run (labelled `automation=<skill>`, default create
  template `automation`), and on later runs clears its chat and re-sends
  `/<skill>` so each run starts fresh.

For the full recipe (adding, changing, or removing a scheduled job, with
copy-paste cron lines), see the `manage-scheduled-tasks` skill -- it is the
canonical guide; this README only describes the pieces.
