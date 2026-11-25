# workspace-update-control

A workspace's updates as a client asks for them, and the dispatcher that starts a
scheduled update and watches over a running one. Standard library only: cron runs
the dispatcher's tick with no venv, since a running update may have broken it.

Everything here acts on the workspace whose root is the current directory, which is
where supervisord runs the shell and where the cron line changes to.

## Modules

- `control` -- one function per operation of the shell's update routes
  (`/api/workspace/update...`): start an update now, arm the schedule, refresh the
  client's values in an armed schedule, cancel it, skip a version, record a
  dismissal, read all of it, and read what tells whether a running update is alive.
  Each takes every value it acts on as an argument (the newest version the client
  knows, a version the user typed, the window in UTC, whether the client holds
  backup credentials). Starting answers at once: the create of the update's chat
  runs on in the background and `data/.state/update-apply/last-start.json` records
  how it went, naming the process that runs it, so an attempt whose process is gone
  reads as `CREATE_FAILED`.
- `dispatcher` -- the minute-by-minute tick: it dispatches an armed schedule inside
  its window, closes out or reminds a run with no verdict (and brings a stopped
  lead back), and holds the run's agents out of the memory guard's way.
- `tick` -- the tick as a program. `system/scripts/update_dispatcher_tick.py` is its
  entry file, which the `update-self-dispatch` cron line runs.
- `agents` -- the workspace's agents read from the events file `mngr observe`
  writes, for the routes, which run no `mngr`. Where the file cannot vouch for the
  agents the answer is "unknown", never "none".
- `protocol` -- the state paths, the outcomes and skip reasons, and the text of the
  messages the dispatcher sends.

## Shared with the Imbue Studio desktop app

The files under `data/.state/update-apply/` (`schedule.json`, `last-dispatch.json`,
`skipped-target.json`, `dismissals.json`, `watchdog.json`, `dispatch-attempt.json`)
and the cron line `update-self-dispatch` are also written by the update program a
desktop from before the workspace's update routes ships into the workspace
(`apps/minds/imbue/minds/workspace_programs/` in the mngr repository). Whichever of
the two wrote the cron line last decides which dispatcher runs, so both must act
the same on the same files: the formats keep `UPDATE_PROGRAM_SCHEMA`, and a fix to
the dispatcher is made in both places, until no supported desktop ships that
program. `last-start.json` is this library's alone.
