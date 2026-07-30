---
name: manage-scheduled-tasks
description: Query and edit the recurring scheduled jobs that run on this host. Use when you (or the user, via you) want to see what is scheduled, add a new recurring job, change when something runs, or stop a job from running. Recurring jobs that must not be missed run through the run_job.sh runner (any cadence from minutes to weeks, with catch-up after downtime and retry of failed runs); exact-time fire-and-forget schedules are ordinary cron lines. Also covers how the built-in weekly Caretaker job is wired (off by default) and where all the scheduling configuration lives.
---

# Managing scheduled tasks

Recurring jobs on this host run through the stock **cron** daemon: what runs
and when is exactly what the drop-in files in `/etc/cron.d/` say. Plain cron
has two failure modes that drive every choice below: **it only fires when the
machine is up at that moment** (a job whose time passes while the container
is off or asleep is skipped, never made up), and **it does not care whether
the job finished** (a run that dies mid-flight is simply gone). When a job
must not be missed, run it through `system/libs/automations/run_job.sh`: an every-minute cron
line ticks it, and it runs the job on its cadence when the machine is up --
catching up the first minute the machine is back after downtime, and
**retrying a run that failed or was killed before completing**. The built-in
weekly **Caretaker** is the worked example of that pattern (see below).

## First: choose the runner or a plain cron line

Pick per job, based on what matters more:

- **`run_job.sh`** -- for any recurring cadence (`--every 15m`, `3h`, `1d`,
  `7d`) that **must not be skipped or half-done**. A cron line ticks every
  minute and hands the decision to the runner, which runs the job once per
  interval: on time when the machine is up, the first minute it is back after
  downtime, and again after a couple of minutes if a run failed or died
  mid-flight. Only a run that **completes** counts.
- **plain cron line** -- for jobs that need an **exact moment** (9:30 on
  Mondays, midnight on the 1st) and where a missed or interrupted run should
  simply not happen. Cron fires exactly on schedule, with the caveats above.

If the user asks for "every N minutes/hours/days" or "daily-ish and
reliable", use the runner. Use a plain line only for "at exactly HH:MM"
jobs -- and if such a job also must not be missed, say so: with a plain line
it will be skipped when the machine is off.

## Timezone: confirm it before scheduling anything

The container's clock is set to the **user's local timezone at each boot** (the
bootstrap fetches it from the minds app on the user's machine). But the user may
have moved since boot, so **when the user asks to schedule something, re-check
their current timezone first**:

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/timezone
# -> {"timezone": "America/Los_Angeles"}   ("" means unknown -- keep the current setting)
cat /etc/timezone                          # what the container currently uses
```

If the boot-time fetch failed, the container is still on UTC -- replace it
with the user's real zone. If they differ, update the container before writing
the schedule entry:

```bash
ln -sf "/usr/share/zoneinfo/<Area/City>" /etc/localtime
echo "<Area/City>" > /etc/timezone
```

Runner jobs pick the change up immediately -- `run_job.sh` reads the clock on
every tick. Precise cron schedule lines additionally need
`supervisorctl restart cron`: the cron daemon caches the timezone it uses to
match those.

## Every job needs the env wrapper

Cron gives jobs a scrubbed, minimal environment -- none of the agent
environment (PATH with `uv`, `MNGR_*`, `LATCHKEY_*`, ...) survives. Prefix
every job command with the wrapper, which rebuilds the workspace environment
from the env files mngr maintains on the host dir and runs the command from
the repo root:

```
/home/user/workspace/system/libs/automations/with_agent_env.sh <command...>
```

Also redirect output to a log file (cron would otherwise try to mail it):
`>> /var/log/supervisor/<job-name>.log 2>&1`.

## Entries live in data/.state/cron.d, installed live to /etc/cron.d

`/etc/cron.d/` sits on the container rootfs, which starts fresh if the
container is ever recreated. Keep each entry's durable copy under
`data/.state/cron.d/<job-name>` (persistent volume; rides the opt-in GitHub
sync) -- the bootstrap reinstalls everything in that directory into
`/etc/cron.d/` at each boot. When adding or editing a job, write the durable
copy first, then make it live:

```
install -m 0644 /home/user/workspace/data/.state/cron.d/<job-name> /etc/cron.d/<job-name>
```

The entry file is still the job's on/off switch -- removing both copies stops
it entirely. Names must be plain (`[A-Za-z0-9_-]` only); cron ignores files
with dots in their names.

## Add a recurring job (catch-up + completion-tracked)

Write the entry (durable copy, then install live, per the section above) with
a line that ticks every minute through the wrapper and the runner:

```
* * * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh /home/user/workspace/system/libs/automations/run_job.sh <job-id> --every <N[mhd]> [--at <hour>] [--retry-after <N[mhd]>] <command...> >> /var/log/supervisor/<job>.log 2>&1
```

- `--every` -- the cadence: `15m`, `3h`, `1d`, `7d`, ...
- `--at <hour>` -- for daily-or-coarser jobs, the local hour (0-23) the run is
  due. Omit for sub-daily cadences.
- `--retry-after` -- gap before retrying a failed or killed run (default 2m).
  Runs themselves take seconds; the gap only matters when something breaks.

The every-minute tick is what makes catch-up possible: the runner exits
instantly on every tick where nothing is due, and a lock held for the run's
whole duration makes overlapping ticks skip. The semantics:

- **Only a completed run covers the window.** The runner records when a run
  starts (`last_attempt`) and, separately, when it exits 0 (`last_success`).
  A run that fails or is killed mid-flight leaves the window due and is
  retried after `--retry-after` -- it can never be silently lost.
- **Runs on time** when the container is up; with `--at`, waits for that hour
  on the day it comes due.
- **Catch-up at any hour:** a window missed while the container was off runs
  within the first minute the container is back -- with `--at`, once a whole
  extra day has passed.
- **Silent when covered:** nothing fires again until the next interval after
  the last *completed* run.
- **Repeated failure escalates:** at 3 consecutive failed attempts the runner
  logs a loud warning in the job's log; the retry cadence is unchanged.

A job with no state yet is due immediately (with `--at`, at that hour). To
make a new job wait one full interval from now instead, seed a completion:
`mkdir -p /home/user/workspace/data/.state/jobs/<job-id> && date +%s > /home/user/workspace/data/.state/jobs/<job-id>/last_success`

## Add a cron job (exact schedule, no catch-up)

Write the entry (same two-copy dance) with a standard 5-field schedule, then
the **user** (always `root` here), then the command:

```
30 9 * * 1   root   /home/user/workspace/system/libs/automations/with_agent_env.sh bash scripts/weekly_report.sh >> /var/log/supervisor/weekly-report.log 2>&1
```

The 5 schedule fields are minute (0-59), hour (0-23), day of month (1-31),
month (1-12), day of week (0-6, Sunday = 0). Common forms: `0 3 * * *` = 3 AM
daily; `0 0 1 * *` = midnight on the 1st. One quirk: `%` is special in cron
commands (means newline) -- escape it as `\%` (e.g. `date +\%F`). Cron
rescans `/etc/cron.d/` within a minute; no reload.

## Set up an automation (run a skill on a schedule)

An **automation** is a skill run automatically on a schedule (the workspace
vocabulary term): a scheduled job that, instead of running a plain script,
wakes a dedicated agent to run one skill in its own chat tab. The machinery
lives in `system/libs/automations/`. To add one -- say a news digest:

1. **Write the skill** at `.agents/skills/<name>/SKILL.md` -- the instructions
   the agent follows on each run (see the existing skills for the shape).
2. **Schedule the shared runner** with the skill name as its argument. Daily
   at 9 AM, or every 15 minutes -- same pattern, different `--every`:

   ```
   * * * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh /home/user/workspace/system/libs/automations/run_job.sh news --every 1d --at 9 bash /home/user/workspace/system/libs/automations/run_automation.sh news >> /var/log/supervisor/news-job.log 2>&1
   * * * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh /home/user/workspace/system/libs/automations/run_job.sh news --every 15m bash /home/user/workspace/system/libs/automations/run_automation.sh news >> /var/log/supervisor/news-job.log 2>&1
   ```

That is all -- no new agent template is required. `system/libs/automations/run_automation.sh
<skill>` creates a persistent singleton agent (labelled `automation=<skill>`),
keeps it alive across runs, and on each run clears its chat and re-sends
`/<skill>`, so the skill runs fresh; the agent surfaces its own chat tab
right after its first message via `system/scripts/layout.py open --layout <desktop|mobile>`
(the same way web apps are surfaced). Pass `--template <t>` only when you want a custom agent
template; otherwise the generic `automation` template is used.

## How the Caretaker is wired (the built-in example)

The Caretaker is the automation pattern above, **off by default**: no
cron entry exists until the user enables it, and even when on, the agent only
wakes when a deterministic check found something. Enabling (the
enable-caretaker skill) writes the single line in
`data/.state/cron.d/minds-caretaker` (installed live to `/etc/cron.d/`):

```
* * * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh /home/user/workspace/system/libs/automations/run_job.sh caretaker --every 7d --at 3 bash /home/user/workspace/system/services/caretaker/caretaker_check.sh >> /var/log/supervisor/caretaker-job.log 2>&1
```

- **Timing** is the standard runner: `--every 7d --at 3`, catch-up after
  downtime, and completion-tracked -- a check that fails or is killed
  mid-run retries within minutes instead of silently losing the week.
- **The deterministic check** looks for services in FATAL/BACKOFF, fresh
  error output in `/var/log/supervisor/` since the last check, disk at or
  above 85 percent, and new OOM-guard shedding. Findings are written to
  `data/.state/caretaker/findings.md` and the Caretaker agent is woken via
  `run_automation.sh caretaker --template caretaker`; with no findings,
  nothing runs until the next weekly check. The one exception: if the agent
  has never introduced itself (no `data/.state/caretaker/permissions.md`), it is
  woken once regardless of findings.
- **On and off:** the entry IS the switch. The enable-caretaker skill writes
  both copies (and clears any stale job state so the introduction lands
  promptly); the disable-caretaker skill removes both, and nothing runs at
  all while disabled. The Caretaker's state under `data/.state/caretaker/`
  survives a disable for a later re-enable.
- **When the agent runs:** at most once a week, at 3 AM local when the
  container is up (first minute back up after an overdue window otherwise),
  and only with findings -- plus the one-time introduction shortly after the
  user enables it.

## See, pause, or remove a job

- **List:** `ls /etc/cron.d/` (live) and `ls /home/user/workspace/data/.state/cron.d/`
  (durable) and read the files -- they are the complete truth about what is
  scheduled.
- **Remove:** delete both copies of the entry file.
- **Pause without losing the definition:** comment the line out with `#` in
  both copies.
- **Check a runner job's state:** read `data/.state/jobs/<job-id>/` --
  `last_success` (epoch of the last completed run), `last_attempt` (epoch of
  the last start), `failures` (consecutive failed attempts, absent when
  healthy). Deleting `last_success` makes the job due again; deleting the
  whole directory resets it entirely.

## Where the configuration lives

The complete map of the scheduling machinery, for edits and debugging:

- `/home/user/workspace/data/.state/cron.d/` -- the durable copy of each entry; the
  bootstrap installs these into `/etc/cron.d/` at each boot.
- `/etc/cron.d/` -- the live drop-ins cron actually reads, one file per job:
  runner jobs are every-minute lines through `run_job.sh`, precise jobs are
  ordinary schedule lines (cron rescans the directory within a minute).
- `/etc/cron.d/minds-caretaker` -- the Caretaker's drop-in (only exists
  while the Caretaker is enabled; see enable-caretaker/disable-caretaker).
- `/home/user/workspace/system/libs/automations/run_job.sh` -- the runner (cadence, catch-up,
  completion tracking, and retry -- with unit tests in
  `system/libs/automations/run_job_test.py`).
- `/home/user/workspace/data/.state/jobs/<job-id>/` -- each runner job's state
  (`last_attempt`, `last_success`, `failures`, `lock`).
- `supervisord.conf` -- `[program:cron]` is the cron daemon (check it with
  `supervisorctl status cron`).
- `/var/log/supervisor/<job>.log` -- each job's own output (per the redirect
  on its entry); `/var/log/supervisor/cron-*.log` -- the cron daemon's logs.
- `/home/user/.mngr/env` and `/home/user/.mngr/agents/<id>/env` -- the host and per-agent env
  files mngr maintains; `system/libs/automations/with_agent_env.sh` sources them (host first,
  then the services agent's) to rebuild the job environment.
- `/etc/localtime` + `/etc/timezone` -- the container clock, set from the
  user's timezone at each boot by the bootstrap (see the timezone section
  above for re-checking it).
