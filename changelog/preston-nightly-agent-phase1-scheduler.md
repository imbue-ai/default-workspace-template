Added a file-driven task scheduler (`libs/scheduler`) that runs recurring shell commands on a cron schedule.

Unlike plain cron, it catches up on runs missed while the machine was off: when the computer comes back online, any task whose scheduled time passed during the downtime runs once (multiple missed intervals collapse into a single run).

The schedule lives in one readable file, `runtime/scheduled_tasks.toml`, that agents and users can edit. Each task has a name, a 5-field cron schedule, a shell command, and `enabled`/`catch_up` flags. A `scheduler` command-line tool lists, adds, shows, and removes tasks; the `scheduler run` daemon executes them.
