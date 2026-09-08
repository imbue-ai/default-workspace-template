---
name: check-app-errors
description: Survey the health of the supervised apps and services on this host and find errors in their logs. Use after building or editing any app/service to confirm it is actually running cleanly, when something seems broken, or when you want a quick read on what (if anything) is currently failing. The Caretaker agent reuses this as its log-scan step.
metadata:
  author: imbue
---

# Checking apps for errors

Background apps and services run under supervisord and write rotated logs to
`/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
This skill surveys those services and their logs to answer "is anything broken,
and if so, what and where?"

Be deliberate with your commands. A few targeted greps across all the logs at
once beat opening files one at a time -- it keeps the survey fast and cheap, which
matters because the Caretaker runs this routinely. Start broad (one grep over
every log), then narrow to the specific services and lines that look wrong.

## Step 1: Survey the services

```bash
supervisorctl status
```

This lists every supervised program and its state. Note anything that is not
`RUNNING`:

- `FATAL` / `BACKOFF` -- the program keeps crashing on startup.
- `EXITED` -- it stopped (expected for a one-shot like `deferred-install`, a
  problem for a long-lived daemon).
- `STARTING` for a long time -- it may be stuck.

The services in `RUNNING` may still be logging errors, so continue to the logs
even when everything looks up.

## Step 2: Scan the logs for errors

Run one broad search across every log first -- stdout and stderr both, since
apps log errors to either, and a scheduled job's own log (under the same
directory) is where its failures live:

```bash
grep -nE 'Traceback|ERROR|Exception|CRITICAL|Fatal' /var/log/supervisor/*.log
```

`-n` prints line numbers so you can jump straight to the context.

Then search for **soft failures**: lines that report a step failing in
lowercase prose, exit 0, and carry on. A pipeline that keeps its last good
copy when a fetch fails is correct on its own, but it makes a dead integration
look exactly like "nothing changed" -- one workspace served eight-day-old data
through a quarter of a million such lines, none of which matched the words
above:

```bash
grep -niE 'failed|failure|no such file or directory|command not found|refused|timed out|keep(ing)? last|skipp(ed|ing)' /var/log/supervisor/*.log
```

A word list cannot be complete, so also read the last few lines of every log
that changed recently and ask whether they describe success or a repeated
failure:

```bash
# which logs changed most recently -- a crash usually just wrote to one
ls -lt /var/log/supervisor/
tail -n 5 /var/log/supervisor/*.log
```

Widen or narrow from there as needed:

```bash
# focus on one service once you know which is misbehaving
tail -n 200 /var/log/supervisor/<name>-stderr.log

# follow a service live while you reproduce the problem
supervisorctl tail -f <name> stderr
```

When a grep hit lands inside a Python traceback, read a window around it (e.g.
`tail`, or open the file at the reported line) -- the final line of the traceback
names the actual exception, and the lines above it show where it came from.

## Step 2b: Reconcile the scheduled jobs

Scheduled jobs are not supervised programs, so `supervisorctl status` says
nothing about them, and a job that has failed on every run still shows a tidy
cron entry. For each entry under the system cron directory (`/etc/cron.d`),
find the log it writes (the redirect at the end of its command line, or the
`manage-scheduled-tasks` skill's convention) and check that its **most recent
run succeeded** -- a last line describing the work done, not an error, and a
timestamp consistent with the schedule.

A job whose log's first line is already a failure has never worked; report it
as such rather than as "logging errors". The common cause is the job's
environment -- a binary the wrapper's `PATH` cannot see, an env var the job
inherited from an older workspace -- so name the failing command verbatim.

## Step 3: Summarize

Report concisely:

- Which services are unhealthy (not `RUNNING`, or `RUNNING` but logging errors),
  which scheduled jobs are not actually succeeding, and which of both are fine.
- For each problem: the error (e.g. the exception type and message), the log file
  and line where it appears, and the likely cause if it is clear from the trace.
- Where you did **not** find anything, so the reader knows the survey was
  thorough.

Keep the summary in plain terms and point at the exact log path for anything you
flag, so the next step (a fix, or a hand-off) starts from a precise location
rather than a re-scan.

## Notes

- `/var/log/supervisor/` logs are container-local and **not** backed up, so treat
  them as transient: capture anything you need to keep elsewhere.
- Logs rotate (`<name>-stderr.log.1`, `.2`, ...). If a recent error is not in the
  live file, check the rotated backups.
- To restart a crashed service after a fix, see the `update-service` skill
  (`supervisorctl restart <name>`).
