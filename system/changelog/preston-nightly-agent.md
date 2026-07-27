Recurring jobs via cron, and the Caretaker. Adds recurring-job scheduling
to every workspace using cron plus a tiny due-checker, and a weekly
**Caretaker** agent built on top of it -- off by default (BETA), gated behind
a deterministic check that wakes the agent only when it finds something --
plus the supporting skills, docs, and in-workspace tab behavior that make
scheduled agents visible.

**Recurring jobs with cron + a completion-tracked runner.** Workspaces
schedule recurring work with **cron** (`/etc/cron.d/` drop-ins, the daemon
running under supervisord as `[program:cron]`): plain cron lines for
exact-moment fire-and-forget jobs, and `system/scripts/run_job.sh` for anything that
must not be skipped or half-done -- an every-minute cron line hands the
decision to the runner, which runs each job once per interval at any cadence
(`--every 15m` to `--every 7d`, with `--at <hour>` for daily-or-coarser
jobs): on time when the container is up, within the first minute the
container is back after a fully-missed window, and never at midnight after a
covered one. Modeled on the host-backup service's tick loop, a window counts
as covered **only when the run completes**: the runner records `last_attempt`
when a run starts and `last_success` only on exit 0 (state under
`data/.state/jobs/`, which survives container recreation and rides the opt-in
GitHub sync), so a run that fails or is killed mid-flight is retried after
`--retry-after` (default 2m) instead of being silently lost, with a loud log
warning after 3 consecutive failures. The runner holds its lock in the
parent and runs the job with the lock fd closed, so daemons a job starts can
never inherit the lock and wedge it. Schedule entries themselves keep a
durable copy under `data/.state/cron.d/`, which the bootstrap reinstalls into
`/etc/cron.d/` at each boot -- so an enabled schedule also survives
container recreation. Plain cron alone provides none of this: it fires only
when the machine is up at that moment, never backfills, and never checks
completion. Because cron scrubs
the job environment, a small wrapper (`system/scripts/with_agent_env.sh`) rebuilds
the workspace environment from the env files mngr maintains on the host dir
(the same way mngr sources them for agent operations), and every scheduled
job runs through it. The container's clock is set to the
user's local timezone at each boot: the bootstrap pulls it from the minds
desktop client's `GET /api/v1/timezone` through the latchkey gateway (falling
back to UTC when unreachable), so schedules run in the user's local time. (An
earlier iteration of this branch built a custom `libs/scheduler` service --
about 635 lines -- for the catch-up behavior; the runner replaces it with
about 130 lines of shell plus a deterministic pytest suite,
`system/scripts/run_job_test.py`.)

**Scheduled agent tasks and the Caretaker.** A scheduled job can wake an agent
that runs a skill on a cadence, in its own chat tab. `system/scripts/run_schedule_agent.sh
<skill>` spawns a single persistent agent for that skill; on each run mngr clears
the agent's session and re-sends `/<skill>` so the skill runs fresh, with no memory
of the previous run.
A new scheduled agent (e.g. a morning news digest) needs only a skill plus a
cron entry -- no new agent template. The weekly **Caretaker** is the built-in
instance -- and it is **off by default, as a BETA feature**: no cron entry
exists until the user turns it on. The new **enable-caretaker** skill (used
only when the user explicitly asks) explains the beta status, gets an
explicit yes, and enables it by writing `/etc/cron.d/minds-caretaker` -- an
ordinary daily-job entry whose weekly due-checker execs
`system/scripts/caretaker_check.sh` when a check is due. Once enabled, the agent
introduces itself shortly afterwards, and from then on each due check runs a
deterministic scan -- services in FATAL/BACKOFF, fresh error output in the
service logs since the last check, disk at or above 85 percent, new OOM-guard
shedding -- and wakes the agent **only when it found something**, telling it
what's up via `data/.state/caretaker/findings.md`. When woken, the Caretaker
verifies the findings, checks basic system health and finished-but-uncommitted
work (committing it, with permission, so it is safely in history), and either
fixes what it found or explains it, always in plain, non-technical language.
On its very first run it does one look-only scan (changing nothing), then
introduces itself and asks whether to keep checking each week and whether to
fix small things on its own. Each run starts from a fresh session (no memory
of the prior run); it remembers your choices and what it saw before through
its own notes on disk, not the conversation. Your standing permissions live in
a single plain-language `data/.state/caretaker/permissions.md` that the Caretaker
reads each run and rewrites when you change your mind, and that you can edit
yourself any time. You stay in full control: the
equally short **disable-caretaker** skill (`rm /etc/cron.d/minds-caretaker`)
switches it off entirely -- while off, nothing runs at all.

**Health-check skills and docs.** Adds a `check-app-errors` skill (survey
`supervisorctl status`, scan `/var/log/supervisor/` for errors and tracebacks,
summarize what's wrong and where), reusable by both day-to-day chat agents and the
Caretaker's weekly scan; and a `manage-scheduled-tasks` skill that teaches agents
to choose between the daily catch-up pattern and plain cron lines per job, the
entry formats, the env wrapper, and to
re-check the user's current timezone (via the minds timezone endpoint) before
scheduling anything, updating the container clock if the user has moved.
The full scheduling detail (daily catch-up vs. plain cron, entry formats, the
env wrapper, the timezone check, the Caretaker wiring) lives in the
manage-scheduled-tasks skill; CLAUDE.md gains just one sentence pointing at the
manage-scheduled-tasks and check-app-errors skills.

**Surfacing scheduled agents in the workspace** needs no UI changes at all:
right after its first message of each run, the woken agent surfaces its own
chat tab (focused) with one best-effort `system/scripts/layout.py open` call -- after
the message, so the tab never pops up empty -- the same existing
mechanism web apps are surfaced with. Doing it from inside the agent avoids
the create-time race where the browser has not yet learned a brand-new agent.

Adds the recurring-job substrate for scheduled work: `system/scripts/run_job.sh`, a completion-tracked runner ticked every minute by cron -- any cadence (`--every 15m` to `7d`, `--at <hour>` for daily-or-coarser), catch-up after downtime, and retry of runs that failed or were killed mid-flight (only a completed run covers a window; state under `data/.state/jobs/`). Durable schedule entries live in `data/.state/cron.d/` and are reinstalled into `/etc/cron.d/` at each boot.

Adds `system/scripts/caretaker_check.sh` (the Caretaker's deterministic weekly check: crashed services, fresh log errors, disk, OOM shedding -- wakes the agent only on findings), `system/scripts/run_schedule_agent.sh` (create-or-retrigger for singleton skill agents), and `system/scripts/with_agent_env.sh` (rebuilds the agent environment for cron jobs from the env files mngr maintains). The cron daemon runs under supervisord as `[program:cron]`; CLAUDE.md gains one pointer sentence.
